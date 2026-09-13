"""Six promises about the lock document itself: what it hashes, what it keeps.

Four of these obligations are about a digest or a document being *stable* under
a change that should not matter, and the awkward part of testing stability is
that a test which never actually varies the input passes for the wrong reason.
`_semantic_config` sorts `consumers` and `external_consumers` before hashing,
but a fixture whose components each name one consumer sorts a one-element list
and proves nothing; `migrate_lockfile` is documented as a metadata rewrite that
never recomputes content, but a fixture carrying three fingerprints and an
empty slices map cannot show that a slice fingerprint or a `component_digests`
map survives; and `parse_lockfile_bytes(dump_lockfile(x))` is trivially the
identity on a stub with no components. So every stability claim here is paired
with a per-example premise that the two spellings really were different
documents, and the permutation property reverses *every* array and *both*
mapping orders rather than shuffling and hoping. A property that draws arrays
of every length still never insists that any one example carried more than one
member, so a hand-built case with a four-element `consumers` list, a
three-element `external_consumers` list and a three-member slice sits beside
the property and pins the multi-element case outright.

The detection halves needed the opposite care. An edge edit that changes no
file is exactly the case the register worries about, so the graph tests build a
real repository, retarget one internal `consumers` edge, and assert that every
component's four fingerprints are byte-identical while `config_digest` moves -
the two assertions have to appear together or neither means anything. Scoped
verify needed a fixture with two slices, one containing the selected component
and one not, because a single slice cannot distinguish "skipped correctly" from
"never compared at all"; the excluded-slice tamper is therefore asserted
absent under `--components x` and asserted *present* under `--components z` and
under no filter at all, three runs over the same tampered lock. Two more slices
are declared by consumer closure rather than by member list, because the rule
is written against *resolved* membership: `closure_of_zulu` names one seed and
resolves to two components, and an implementation that read the declared list
would skip it under `--components xray` while looking entirely correct.

Two derivations are read at runtime rather than listed. The diff coverage test
enumerates `COMPONENT_METADATA_FIELDS` itself and mutates each member in turn,
with a table of type-valid alternates that must cover the constant exactly, so
a field added to the constant without an alternate fails here instead of
silently going unexercised; and because the validator's accepted set is
*derived* from that same constant in `_lockfile.py`'s wrapper, the independent
witness is the published `spec/boundary.lock.schema.json`, whose component
properties are compared against the constant as a set. The schema-annotation
ratchet is likewise read from both sides: the ref inside `LOCKFILE_SCHEMA_URL`
is parsed with the release script's own regex and compared against the release
script's own `CANONICAL_LOCK_SCHEMA_REF`, so the two constants cannot drift
apart while both tests pass.

Covers OBL-HASHING-053, OBL-HASHING-057, OBL-HASHING-058, OBL-HASHING-059,
OBL-HASHING-060 and OBL-HASHING-061.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest import mock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import boundver
from boundver import core
from boundver._consumer_graph import resolve_slice_components
from boundver._diff import diff_lockfiles
from boundver._lockfile import (
    COMPONENT_METADATA_FIELDS,
    LOCKFILE_SCHEMA,
    LOCKFILE_SCHEMA_URL,
    SEMANTIC_CONFIG_VERSION,
    MigrationError,
    _lockfile_structure_issues,
    dump_lockfile,
    generate_lockfile,
    migrate_lockfile,
    parse_lockfile_bytes,
    semantic_config_digest,
)
from boundver._utils import ConfigError, FACETS
from boundver.providers import ResolvedBoundary

from tests._parity import run_cli, run_cli_in_process
from tests._scenarios import Scenario

REPO_ROOT = Path(__file__).resolve().parents[1]

PROFILE = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


# ---------------------------------------------------------------------------
# OBL-HASHING-053: the consumer graph, permuted and edited
# ---------------------------------------------------------------------------

#: Component names the graph strategy draws from. Five is enough that a
#: `consumers` array can hold four members, which is what makes a permutation
#: of it a real permutation rather than a relabelling of one element.
GRAPH_NAMES = ("alpha", "beta", "gamma", "delta", "epsilon")

#: External consumer identifiers. These are opaque strings rather than
#: component names, which is the whole point of the second array.
EXTERNAL_NAMES = ("team-payments", "team-search", "partner-x", "partner-y")

#: The two edge-bearing arrays, and the alphabet each one's targets come from.
EDGE_FIELDS = {
    "consumers": GRAPH_NAMES,
    "external_consumers": EXTERNAL_NAMES,
}


class Graph:
    """One consumer graph, with the declaration order it was drawn in."""

    def __init__(
        self,
        names: List[str],
        edges: Dict[str, Dict[str, List[str]]],
        slices: List[Tuple[str, List[str]]],
    ) -> None:
        self.names = names
        self.edges = edges
        self.slices = slices

    def normal_form(self) -> frozenset:
        """The graph as a set of labelled edges, independent of any ordering.

        This is the oracle. It is a reading of the obligation - "invariant
        under permutation, changed by an add, a remove or a retarget" - and not
        a copy of `_semantic_config`, which sorts lists and would therefore
        agree with a multiset rather than with a set. The strategy draws
        duplicate-free arrays so the two coincide; the case where they do not
        is pinned separately by
        `test_a_repeated_consumer_entry_is_not_a_permutation`.
        """
        members = {("component", name) for name in self.names}
        for owner, fields in self.edges.items():
            for field, targets in fields.items():
                members.update((owner, field, target) for target in targets)
        for name, components in self.slices:
            members.add((name, "slice", frozenset(components)))
        return frozenset(members)

    def render(self, *, reverse: bool = False) -> dict:
        """Spell the graph as a config, optionally with every order flipped."""
        names = list(reversed(self.names)) if reverse else list(self.names)
        components: Dict[str, Any] = {}
        for name in names:
            entry: Dict[str, Any] = {
                "path": f"services/{name}",
                "boundary": {"provider": "path-hash", "paths": ["api.txt"]},
            }
            for field in EDGE_FIELDS:
                targets = list(self.edges[name][field])
                entry[field] = list(reversed(targets)) if reverse else targets
            components[name] = entry
        declared = list(reversed(self.slices)) if reverse else list(self.slices)
        slices: Dict[str, Any] = {}
        for slice_name, members in declared:
            ordered = list(reversed(members)) if reverse else list(members)
            slices[slice_name] = {"mode": "exact", "components": ordered}
        config: Dict[str, Any] = {"project": "graph", "components": components}
        if slices:
            config["slices"] = slices
        return config


@st.composite
def graphs(draw) -> Graph:
    """A small consumer graph with duplicate-free edge arrays."""
    names = draw(
        st.lists(st.sampled_from(GRAPH_NAMES), min_size=2, max_size=5, unique=True)
    )
    edges: Dict[str, Dict[str, List[str]]] = {}
    for name in names:
        edges[name] = {}
        for field, alphabet in EDGE_FIELDS.items():
            pool = names if field == "consumers" else list(alphabet)
            edges[name][field] = draw(
                st.lists(
                    st.sampled_from(pool),
                    max_size=len(pool),
                    unique=True,
                )
            )
    slice_count = draw(st.integers(min_value=0, max_value=2))
    slices: List[Tuple[str, List[str]]] = []
    for index in range(slice_count):
        members = draw(
            st.lists(st.sampled_from(names), min_size=1, max_size=len(names), unique=True)
        )
        slices.append((f"slice{index}", members))
    return Graph(names, edges, slices)


def _edge_mutations(graph: Graph) -> List[tuple]:
    """Every add, remove and retarget available on this graph."""
    options: List[tuple] = []
    for owner in graph.names:
        for field, alphabet in EDGE_FIELDS.items():
            pool = list(graph.names) if field == "consumers" else list(alphabet)
            present = list(graph.edges[owner][field])
            absent = [target for target in pool if target not in present]
            for target in present:
                options.append(("remove", owner, field, target, None))
            for target in absent:
                options.append(("add", owner, field, target, None))
            for target in present:
                for replacement in absent:
                    options.append(
                        ("retarget", owner, field, target, replacement)
                    )
    return options


def _apply_mutation(graph: Graph, mutation: tuple) -> Graph:
    kind, owner, field, target, replacement = mutation
    edges = {
        name: {key: list(value) for key, value in fields.items()}
        for name, fields in graph.edges.items()
    }
    targets = edges[owner][field]
    if kind == "remove":
        targets.remove(target)
    elif kind == "add":
        targets.append(target)
    else:
        targets[targets.index(target)] = replacement
    return Graph(list(graph.names), edges, list(graph.slices))


@st.composite
def graphs_with_one_edge_mutation(draw):
    graph = draw(graphs())
    options = _edge_mutations(graph)
    mutation = draw(st.sampled_from(options))
    return graph, mutation


class ConsumerGraphDigestTests(unittest.TestCase):
    """OBL-HASHING-053: reordering is free, rewiring is not."""

    @staticmethod
    def _spelling(config: dict) -> str:
        """The document as written, preserving every insertion order."""
        return json.dumps(config, sort_keys=False, ensure_ascii=False)

    @given(graphs())
    @PROFILE
    def test_permuting_the_graph_leaves_semantic_config_digest_alone(self, graph):
        forward = graph.render()
        flipped = graph.render(reverse=True)
        # The premise, checked on every example rather than once: the two
        # spellings really are different documents. Two or more components are
        # drawn, so reversing the component order always changes the text.
        self.assertNotEqual(
            self._spelling(forward),
            self._spelling(flipped),
            "the reversed spelling was identical, so nothing was permuted",
        )
        self.assertEqual(
            semantic_config_digest(forward), semantic_config_digest(flipped)
        )

    @given(graphs_with_one_edge_mutation())
    @PROFILE
    def test_one_added_removed_or_retargeted_edge_always_moves_the_digest(
        self, drawn
    ):
        graph, mutation = drawn
        mutated = _apply_mutation(graph, mutation)
        # The premise: the mutation really changed the graph, judged by an
        # oracle that never calls into boundver.
        self.assertNotEqual(
            graph.normal_form(),
            mutated.normal_form(),
            f"mutation {mutation} left the edge set unchanged",
        )
        self.assertNotEqual(
            semantic_config_digest(graph.render()),
            semantic_config_digest(mutated.render()),
            f"mutation {mutation} did not move the digest",
        )

    def test_a_multi_element_array_is_the_case_the_sorting_exists_for(self):
        """The register's complaint, stated directly rather than sampled.

        The property above draws arrays of every length, but nothing in it
        insists an example carried more than one consumer, and a one-element
        list sorts to itself. This case has four internal consumers, three
        external ones and a three-member slice, and differs from its twin in
        nothing but the order of those three lists.
        """
        forward = {
            "project": "p",
            "components": {
                "hub": {
                    "path": "hub",
                    "consumers": ["alpha", "beta", "gamma", "delta"],
                    "external_consumers": ["partner-x", "team-search", "partner-y"],
                },
            },
            "slices": {"s": {"mode": "exact", "components": ["gamma", "alpha", "beta"]}},
        }
        shuffled = copy.deepcopy(forward)
        component = shuffled["components"]["hub"]
        component["consumers"] = ["gamma", "delta", "alpha", "beta"]
        component["external_consumers"] = ["team-search", "partner-y", "partner-x"]
        shuffled["slices"]["s"]["components"] = ["beta", "gamma", "alpha"]
        # The premise: three genuinely reordered multi-element arrays.
        for field in ("consumers", "external_consumers"):
            self.assertNotEqual(
                forward["components"]["hub"][field], component[field]
            )
            self.assertGreater(len(component[field]), 1)
        self.assertNotEqual(
            forward["slices"]["s"]["components"],
            shuffled["slices"]["s"]["components"],
        )
        self.assertEqual(
            semantic_config_digest(forward), semantic_config_digest(shuffled)
        )

    def test_every_graph_offers_at_least_one_edge_mutation(self):
        """The premise for the property above: the sampler is never empty."""
        smallest = Graph(
            ["alpha", "beta"],
            {
                "alpha": {"consumers": [], "external_consumers": []},
                "beta": {"consumers": [], "external_consumers": []},
            },
            [],
        )
        saturated = Graph(
            ["alpha", "beta"],
            {
                name: {
                    "consumers": ["alpha", "beta"],
                    "external_consumers": list(EXTERNAL_NAMES),
                }
                for name in ("alpha", "beta")
            },
            [],
        )
        for label, graph in (("empty", smallest), ("saturated", saturated)):
            with self.subTest(graph=label):
                kinds = {option[0] for option in _edge_mutations(graph)}
                self.assertTrue(kinds, "no mutation was available")
        self.assertEqual(
            {option[0] for option in _edge_mutations(smallest)}, {"add"}
        )
        self.assertEqual(
            {option[0] for option in _edge_mutations(saturated)}, {"remove"}
        )

    def test_an_internal_edge_edit_moves_the_digest_while_fingerprints_hold(self):
        """The case the obligation names: routing changed, content did not."""
        with Scenario("graph053") as scene:
            scene.component(
                "alpha", path="alpha", provider="path-hash",
                boundary=["api.txt"], consumers=["beta"],
            )
            scene.component(
                "beta", path="beta", provider="path-hash", boundary=["api.txt"]
            )
            scene.component(
                "gamma", path="gamma", provider="path-hash", boundary=["api.txt"]
            )
            for name in ("alpha", "beta", "gamma"):
                scene.file(f"{name}/api.txt", f"{name}\n")
            scene.commit()
            before = generate_lockfile(scene.config, scene.root, source="working-tree")
            edits = {
                "retargeted": ["gamma"],
                "removed": [],
                "added": ["beta", "gamma"],
            }
            for label, consumers in edits.items():
                with self.subTest(edge_edit=label):
                    scene.config["components"]["alpha"]["consumers"] = list(consumers)
                    after = generate_lockfile(
                        scene.config, scene.root, source="working-tree"
                    )
                    self.assertEqual(
                        {n: e["fingerprints"] for n, e in before["components"].items()},
                        {n: e["fingerprints"] for n, e in after["components"].items()},
                        "the edge edit changed a component fingerprint too",
                    )
                    self.assertNotEqual(
                        before["config_digest"], after["config_digest"]
                    )
            scene.config["components"]["alpha"]["consumers"] = ["beta"]
            restored = generate_lockfile(scene.config, scene.root, source="working-tree")
            self.assertEqual(before["config_digest"], restored["config_digest"])

    def test_a_repeated_consumer_entry_is_not_a_permutation(self):
        """Pinned, because the sorted projection keeps multiplicity.

        `_semantic_config` sorts rather than de-duplicates, so ``["b", "b"]``
        and ``["b"]`` hash differently even though they name the same edge. The
        set-valued oracle above would call them equal, which is why the graph
        strategy draws duplicate-free arrays and this case is stated by hand.
        """
        single = {
            "project": "p",
            "components": {"a": {"path": "a", "consumers": ["b"]}, "b": {"path": "b"}},
        }
        repeated = copy.deepcopy(single)
        repeated["components"]["a"]["consumers"] = ["b", "b"]
        self.assertNotEqual(
            semantic_config_digest(single), semantic_config_digest(repeated)
        )


# ---------------------------------------------------------------------------
# OBL-HASHING-057: what a scoped verify may skip, and what it may not
# ---------------------------------------------------------------------------

#: A digest-shaped value nothing in the fixture computes, used to tamper with a
#: recorded fingerprint without tripping the "must be a SHA-256 digest" check.
FOREIGN_DIGEST = "b" * 64


def _drop_component(lock: dict) -> None:
    lock["components"].pop("zulu")


def _rename_project(lock: dict) -> None:
    lock["project"] = "somebody-elses-project"


def _drop_slice(lock: dict) -> None:
    lock["slices"].pop("without_xray")


def _plant_generation_error(lock: dict) -> None:
    lock["components"]["zulu"]["boundary_status"] = "error"
    lock["components"]["zulu"]["boundary_errors"] = ["planted provider failure"]


#: The four lock-wide preflight invariants, each with the tamper that breaks it
#: and the exact message observed from `_verify_lock_preflight_issues`. Every
#: one of these is asserted under `components=["xray"]`, and every tamper is on
#: something outside that selection.
PREFLIGHT_TAMPERS = {
    "component set": (
        _drop_component,
        "LOCKFILE component set differs from config: "
        "locked=['xray', 'yankee'] configured=['xray', 'yankee', 'zulu']",
    ),
    "project identity": (
        _rename_project,
        "METADATA MISMATCH project: lockfile='somebody-elses-project' "
        "current='scope057'",
    ),
    "slice set": (
        _drop_slice,
        "LOCKFILE slice set differs from config: "
        "locked=['closure_of_yankee', 'closure_of_zulu', 'with_xray'] "
        "configured=['closure_of_yankee', 'closure_of_zulu', 'with_xray', "
        "'without_xray']",
    ),
    "locked digest error": (
        _plant_generation_error,
        "LOCKED DIGEST ERROR zulu: planted provider failure",
    ),
}


class ScopedVerifyPreflightTests(unittest.TestCase):
    """OBL-HASHING-057: a subset of components is not a subset of the lock."""

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario("scope057")
        scene = cls.scene
        for name in ("xray", "yankee", "zulu"):
            scene.component(
                name, path=name, provider="path-hash", boundary=["api.txt"]
            )
            scene.file(f"{name}/api.txt", f"{name}\n")
        # zulu consumes xray, which is what gives `closure_of_zulu` a resolved
        # membership wider than the one it declares.
        scene.config["components"]["zulu"]["consumers"] = ["xray"]
        # Two explicit slices, deliberately: one slice cannot tell a correct
        # skip from a comparison that never happened. Two closure slices on top
        # of them, because the rule is about *resolved* membership and a
        # closure slice names one seed while resolving to several components.
        scene.slice("with_xray", mode="exact", components=["xray", "yankee"])
        scene.slice("without_xray", mode="exact", components=["yankee", "zulu"])
        scene.slice("closure_of_zulu", mode="exact", closure_of="zulu")
        scene.slice("closure_of_yankee", mode="exact", closure_of="yankee")
        scene.commit()
        cls.clean_lock = generate_lockfile(
            scene.config, scene.root, source="working-tree"
        )

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()

    def _verify(self, mutate, components):
        """Write a tampered copy of the clean lock and verify it."""
        lock = copy.deepcopy(self.clean_lock)
        if mutate is not None:
            mutate(lock)
        (self.scene.root / "boundary.lock.json").write_text(
            dump_lockfile(lock), encoding="utf-8"
        )
        previous = Path.cwd()
        try:
            os.chdir(self.scene.root)
            return boundver.verify(
                "boundary.config.json",
                "boundary.lock.json",
                source="working-tree",
                components=components,
            )
        finally:
            os.chdir(previous)

    def test_the_untampered_lock_verifies_clean_both_scoped_and_whole(self):
        """The premise for every absence below: this fixture starts current."""
        self.assertEqual(self._verify(None, ["xray"]), [])
        self.assertEqual(self._verify(None, None), [])

    def test_every_lock_wide_preflight_invariant_fires_under_a_component_filter(self):
        for label, (mutate, expected) in PREFLIGHT_TAMPERS.items():
            with self.subTest(invariant=label):
                self.assertEqual(self._verify(mutate, ["xray"]), [expected])

    def test_a_slice_that_excludes_the_selection_is_not_compared(self):
        def tamper(lock):
            lock["slices"]["without_xray"]["fingerprint"] = FOREIGN_DIGEST

        self.assertEqual(self._verify(tamper, ["xray"]), [])

    def test_the_excluded_slice_tamper_is_reported_when_it_is_in_scope(self):
        """The premise: the skipped comparison would have failed if it ran."""

        def tamper(lock):
            lock["slices"]["without_xray"]["fingerprint"] = FOREIGN_DIGEST

        for label, components in (("unfiltered", None), ("a member", ["zulu"])):
            with self.subTest(scope=label):
                issues = self._verify(tamper, components)
                self.assertEqual(len(issues), 1, issues)
                self.assertTrue(
                    issues[0].startswith(
                        "SLICE MISMATCH without_xray.exact: lockfile=bbbbbbbbbbbb..."
                    ),
                    issues[0],
                )

    def test_a_slice_that_includes_the_selection_is_compared_even_when_it_is_clean(self):
        def tamper(lock):
            lock["slices"]["with_xray"]["fingerprint"] = FOREIGN_DIGEST

        issues = self._verify(tamper, ["xray"])
        self.assertEqual(len(issues), 1, issues)
        self.assertTrue(
            issues[0].startswith(
                "SLICE MISMATCH with_xray.exact: lockfile=bbbbbbbbbbbb..."
            ),
            issues[0],
        )

    def test_the_slice_fixture_resolves_the_memberships_these_tests_assume(self):
        """The premise: four slices, and the closures really do differ.

        `closure_of_zulu` declares one seed and resolves to two components, one
        of which is the selected one. An implementation that read the declared
        `components` list instead of resolving the closure would skip it, and
        the skip would look correct.
        """
        components = self.scene.config["components"]
        resolved = {
            name: resolve_slice_components(definition, components)
            for name, definition in self.scene.config["slices"].items()
        }
        self.assertEqual(
            resolved,
            {
                "with_xray": ["xray", "yankee"],
                "without_xray": ["yankee", "zulu"],
                "closure_of_zulu": ["xray", "zulu"],
                "closure_of_yankee": ["yankee"],
            },
        )
        self.assertNotIn(
            "xray", self.scene.config["slices"]["closure_of_zulu"].get("components", [])
        )

    def test_a_closure_slice_is_compared_when_the_closure_reaches_the_selection(self):
        def tamper(lock):
            lock["slices"]["closure_of_zulu"]["fingerprint"] = FOREIGN_DIGEST

        issues = self._verify(tamper, ["xray"])
        self.assertEqual(len(issues), 1, issues)
        self.assertTrue(
            issues[0].startswith(
                "SLICE MISMATCH closure_of_zulu.exact: lockfile=bbbbbbbbbbbb..."
            ),
            issues[0],
        )

    def test_a_closure_slice_the_closure_never_reaches_is_not_compared(self):
        def tamper(lock):
            lock["slices"]["closure_of_yankee"]["fingerprint"] = FOREIGN_DIGEST

        self.assertEqual(self._verify(tamper, ["xray"]), [])
        # The premise: the same tamper is reported for the one component the
        # closure does resolve to.
        issues = self._verify(tamper, ["yankee"])
        self.assertEqual(len(issues), 1, issues)
        self.assertTrue(
            issues[0].startswith(
                "SLICE MISMATCH closure_of_yankee.exact: lockfile=bbbbbbbbbbbb..."
            ),
            issues[0],
        )

    def test_the_selected_component_itself_is_clean_in_every_slice_case(self):
        """The premise for "even when X itself is clean": xray never drifts.

        Both slice cases above leave every component fingerprint exactly as
        generated, so the only thing a scoped run could report is the slice
        aggregate. Without this the including-slice test could be passing on a
        component mismatch that happens to be reported first.
        """
        for slice_name in ("with_xray", "without_xray"):
            with self.subTest(tampered_slice=slice_name):
                lock = copy.deepcopy(self.clean_lock)
                lock["slices"][slice_name]["fingerprint"] = FOREIGN_DIGEST
                current = generate_lockfile(
                    self.scene.config, self.scene.root, source="working-tree"
                )
                self.assertEqual(
                    lock["components"], current["components"],
                    "the tamper disturbed a component entry",
                )

    def test_the_cli_surface_honours_the_same_two_rules(self):
        """`verify --components X`, as the obligation spells it."""
        lock = copy.deepcopy(self.clean_lock)
        lock["slices"]["without_xray"]["fingerprint"] = FOREIGN_DIGEST
        (self.scene.root / "boundary.lock.json").write_text(
            dump_lockfile(lock), encoding="utf-8"
        )
        scoped_away = run_cli(
            self.scene.root, "verify", "--source", "working-tree",
            "--components", "xray",
        )
        self.assertEqual(scoped_away.returncode, 0, scoped_away.stdout)
        self.assertIn("Lockfile is up to date.", scoped_away.stdout)

        in_scope = run_cli(
            self.scene.root, "verify", "--source", "working-tree",
            "--components", "zulu",
        )
        self.assertEqual(in_scope.returncode, 1, in_scope.stdout)
        self.assertIn("SLICE MISMATCH without_xray.exact", in_scope.stdout)

        missing = copy.deepcopy(self.clean_lock)
        _drop_component(missing)
        (self.scene.root / "boundary.lock.json").write_text(
            dump_lockfile(missing), encoding="utf-8"
        )
        preflight = run_cli(
            self.scene.root, "verify", "--source", "working-tree",
            "--components", "xray",
        )
        self.assertEqual(preflight.returncode, 2, preflight.stderr)
        self.assertIn("ERROR: lockfile preflight failed:", preflight.stderr)
        self.assertIn(
            "LOCKFILE component set differs from config: "
            "locked=['xray', 'yankee'] configured=['xray', 'yankee', 'zulu']",
            preflight.stderr,
        )


# ---------------------------------------------------------------------------
# OBL-HASHING-058: every persisted component field, enumerated
# ---------------------------------------------------------------------------

#: A structurally valid replacement value for each member of
#: COMPONENT_METADATA_FIELDS. The table is keyed by field name and is asserted
#: to cover the constant exactly, so a field added to the constant fails here
#: rather than quietly going unexercised. Every value is chosen to keep the
#: mutated lock acceptable to `_lockfile_structure_issues`, which is the half
#: of the obligation that says these are fields validation accepts.
FIELD_ALTERNATES = {
    "version": "9.9.9",
    "path": "somewhere-else",
    "boundary_provider": "leaf",
    "boundary_provider_version": "99",
    "boundary_status": "partial",
    "semver": {"compat_family": "9", "api_surface": None, "exact_version": None},
    "consumers": ["a-new-consumer"],
    "external_consumers": ["a-new-external-consumer"],
    "boundary_metadata": {"operations": {"nested": ["GET /ping"]}},
    "version_errors": ["planted"],
    "exact_errors": ["planted"],
    "behavior_errors": ["planted"],
    "boundary_errors": ["planted"],
    "warnings": ["planted"],
    "vendored_copies": ["vendor/copy"],
    "vendored_digests": {"vendor/copy": "d" * 64},
    "vendored_errors": ["planted"],
}


class ComponentMetadataFieldCoverageTests(unittest.TestCase):
    """OBL-HASHING-058: the diff enumerates what the lock persists."""

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario("metadata058")
        scene = cls.scene
        for name in ("svc", "other"):
            scene.component(
                name, path=name, provider="path-hash", boundary=["api.txt"]
            )
            scene.file(f"{name}/api.txt", f"{name}\n")
        scene.commit()
        cls.base = generate_lockfile(scene.config, scene.root, source="working-tree")
        with (REPO_ROOT / "spec" / "boundary.lock.schema.json").open(
            encoding="utf-8"
        ) as handle:
            cls.published_schema = json.load(handle)

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()

    def test_the_alternate_table_covers_the_constant_exactly(self):
        """The enumeration is over the constant, read now, not over this table."""
        self.assertEqual(set(FIELD_ALTERNATES), set(COMPONENT_METADATA_FIELDS))
        self.assertEqual(
            len(COMPONENT_METADATA_FIELDS),
            len(set(COMPONENT_METADATA_FIELDS)),
            "COMPONENT_METADATA_FIELDS repeats a name",
        )
        self.assertNotIn("fingerprints", COMPONENT_METADATA_FIELDS)

    def test_changing_exactly_one_persisted_field_reports_the_component_changed(self):
        for field in COMPONENT_METADATA_FIELDS:
            with self.subTest(field=field):
                new = copy.deepcopy(self.base)
                replacement = FIELD_ALTERNATES[field]
                self.assertNotEqual(
                    new["components"]["svc"].get(field),
                    replacement,
                    "the alternate equals the generated value, so nothing changed",
                )
                new["components"]["svc"][field] = replacement
                result = diff_lockfiles(self.base, new)
                changed = result["components"]["changed"]
                self.assertEqual([entry["name"] for entry in changed], ["svc"])
                self.assertEqual(result["components"]["unchanged"], ["other"])
                self.assertEqual(changed[0]["changed_facets"], {})
                self.assertEqual(
                    changed[0]["changed_metadata"].get(field),
                    {"old": self.base["components"]["svc"].get(field),
                     "new": replacement},
                )
                self.assertEqual(changed[0]["summary"], "component metadata changed")

    def test_each_mutated_lock_is_one_the_validator_still_accepts(self):
        """The "accepted by lockfile_structure_issues" half, field by field."""
        for field in COMPONENT_METADATA_FIELDS:
            with self.subTest(field=field):
                new = copy.deepcopy(self.base)
                new["components"]["svc"][field] = FIELD_ALTERNATES[field]
                self.assertEqual(_lockfile_structure_issues(new), [])

    def test_a_field_the_constant_does_not_name_is_rejected(self):
        """The premise: acceptance above is a decision, not indifference."""
        new = copy.deepcopy(self.base)
        new["components"]["svc"]["future_field"] = "x"
        self.assertEqual(
            _lockfile_structure_issues(new),
            ["LOCKFILE malformed: unknown field in component 'svc': future_field"],
        )
        # And the diff would not have reported it, which is why the set
        # identity below matters rather than the enumeration alone.
        self.assertEqual(
            diff_lockfiles(self.base, new)["components"]["unchanged"],
            ["other", "svc"],
        )

    def test_the_published_lock_schema_names_exactly_these_component_fields(self):
        """The independent witness.

        `_lockfile.py`'s wrapper hands `lockfile_structure_issues` the same
        constant the diff reads, so comparing those two is a tautology. The
        published JSON Schema in `spec/` is maintained separately and closes
        `additionalProperties`, so it is the surface that can actually drift.
        """
        component = self.published_schema["properties"]["components"][
            "additionalProperties"
        ]
        self.assertIs(component["additionalProperties"], False)
        self.assertEqual(
            set(component["properties"]),
            set(COMPONENT_METADATA_FIELDS) | {"fingerprints"},
        )

    def test_every_key_a_generated_component_entry_carries_is_enumerated(self):
        """A field generation emits but the diff skips is the failure mode."""
        emitted = set()
        for entry in self.base["components"].values():
            emitted.update(entry)
        self.assertTrue(emitted)
        self.assertEqual(
            emitted - {"fingerprints"} - set(COMPONENT_METADATA_FIELDS), set()
        )
        self.assertEqual(set(self.base["components"]["svc"]["fingerprints"]), set(FACETS))


# ---------------------------------------------------------------------------
# A lock document model, shared by OBL-HASHING-059 and OBL-HASHING-061
# ---------------------------------------------------------------------------

#: Root keys `generate_lockfile` emits, pinned against a real lock by
#: `test_the_model_matches_the_shape_generation_emits`.
LOCK_ROOT_KEYS = frozenset(
    {"$schema", "schema", "config_contract", "config_digest", "project",
     "components", "slices"}
)

#: Component keys every generated entry carries.
REQUIRED_COMPONENT_KEYS = frozenset(
    {"version", "path", "boundary_provider", "boundary_provider_version",
     "boundary_status", "consumers", "external_consumers", "fingerprints",
     "semver"}
)

#: Component keys generation emits only when it has something to say. Their
#: absence is part of the round-trip claim: an omitted key must come back
#: omitted, not as an explicit null.
OPTIONAL_ARRAY_KEYS = (
    "version_errors", "exact_errors", "behavior_errors", "boundary_errors",
    "warnings", "vendored_copies", "vendored_errors",
)

SLICE_KEYS = frozenset(
    {"description", "mode", "components", "fingerprint", "component_digests"}
)

_digests = st.text("0123456789abcdef", min_size=64, max_size=64)
#: Text drawn from everything UTF-8 can encode, which is the acceptance rule
#: `_bounded_json_value_issues` applies. Surrogates are excluded because a lone
#: surrogate cannot be encoded and generation refuses one, as
#: `test_metadata_that_would_break_the_round_trip_is_refused_at_generation`
#: shows.
_labels = st.text(st.characters(codec="utf-8"), min_size=1, max_size=8)
_json_values = st.recursive(
    st.none() | st.booleans() | st.integers(-(10 ** 9), 10 ** 9) | _labels,
    lambda children: st.lists(children, max_size=3)
    | st.dictionaries(_labels, children, max_size=3),
    max_leaves=5,
)


@st.composite
def component_entries(draw) -> dict:
    entry: Dict[str, Any] = {
        "version": draw(st.none() | _labels),
        "path": draw(_labels),
        "boundary_provider": draw(_labels),
        "boundary_provider_version": draw(st.none() | _labels),
        "boundary_status": draw(st.sampled_from(("ok", "partial", "error"))),
        "consumers": draw(st.lists(_labels, max_size=3, unique=True)),
        "external_consumers": draw(st.lists(_labels, max_size=3, unique=True)),
        "fingerprints": {
            facet: draw(st.none() | _digests) for facet in FACETS
        },
        "semver": {
            field: draw(st.none() | _labels)
            for field in ("compat_family", "api_surface", "exact_version")
        },
    }
    for field in OPTIONAL_ARRAY_KEYS:
        if draw(st.booleans()):
            entry[field] = draw(st.lists(_labels, max_size=2))
    if draw(st.booleans()):
        entry["boundary_metadata"] = draw(
            st.dictionaries(_labels, _json_values, max_size=3)
        )
    if draw(st.booleans()):
        entry["vendored_digests"] = draw(
            st.dictionaries(_labels, _digests, max_size=2)
        )
    return entry


@st.composite
def lock_documents(draw) -> dict:
    names = draw(st.lists(_labels, min_size=1, max_size=3, unique=True))
    slice_names = draw(st.lists(_labels, max_size=2, unique=True))
    slices: Dict[str, Any] = {}
    for slice_name in slice_names:
        members = draw(st.lists(st.sampled_from(names), max_size=3, unique=True))
        slices[slice_name] = {
            "description": draw(_labels | st.just("")),
            "mode": draw(st.sampled_from(FACETS)),
            "components": members,
            "fingerprint": draw(_digests),
            "component_digests": {
                member: draw(st.none() | _digests) for member in members
            },
        }
    return {
        "$schema": LOCKFILE_SCHEMA_URL,
        "schema": LOCKFILE_SCHEMA,
        "config_contract": SEMANTIC_CONFIG_VERSION,
        "config_digest": draw(_digests),
        "project": draw(_labels),
        "components": {name: draw(component_entries()) for name in names},
        "slices": slices,
    }


# ---------------------------------------------------------------------------
# OBL-HASHING-059: migration is a metadata rewrite, nothing more
# ---------------------------------------------------------------------------


def _migration_oracle(lockfile: dict) -> dict:
    """The three documented edits, spelled out independently of the code."""
    expected = {
        key: value for key, value in lockfile.items() if key != "generated_at"
    }
    expected["schema"] = LOCKFILE_SCHEMA
    expected.setdefault("components", {})
    expected.setdefault("slices", {})
    return expected


class MigrationDigestNeutralityTests(unittest.TestCase):
    """OBL-HASHING-059: every digest survives a migration byte for byte."""

    @given(
        lock_documents(),
        st.booleans(),
        st.sampled_from(((), ("slices",))),
    )
    @PROFILE
    def test_migration_is_the_input_plus_exactly_three_documented_edits(
        self, lockfile, stamp, dropped
    ):
        if stamp:
            lockfile["generated_at"] = "2020-01-01T00:00:00Z"
        for key in dropped:
            lockfile.pop(key)
        before = copy.deepcopy(lockfile)
        migrated = migrate_lockfile(lockfile)
        self.assertEqual(migrated, _migration_oracle(before))
        self.assertEqual(
            lockfile, before, "migrate_lockfile mutated the lock it was given"
        )

    @given(lock_documents())
    @PROFILE
    def test_every_digest_field_is_byte_identical_after_migration(self, lockfile):
        """Compared against an independent snapshot, not against the input.

        `migrate_lockfile` returns `dict(lockfile)`, so the result's
        `components` and `slices` are the very objects the input holds. Reading
        a fingerprint out of both and calling `assertEqual` would compare an
        object with itself and pass however the function behaved, which is why
        the reference here is a deep copy taken before the call.
        """
        lockfile["generated_at"] = "2020-01-01T00:00:00Z"
        reference = copy.deepcopy(lockfile)
        migrated = migrate_lockfile(lockfile)
        self.assertEqual(migrated["config_digest"], reference["config_digest"])
        for name, entry in reference["components"].items():
            self.assertEqual(
                migrated["components"][name]["fingerprints"], entry["fingerprints"]
            )
        for name, entry in reference["slices"].items():
            self.assertEqual(
                migrated["slices"][name]["fingerprint"], entry["fingerprint"]
            )
            self.assertEqual(
                migrated["slices"][name]["component_digests"],
                entry["component_digests"],
            )

    def test_the_migrated_lock_shares_its_nested_values_with_the_input(self):
        """The premise for the deep copy above: the copy really is shallow.

        Pinned rather than judged. Nothing in the obligation asks for a deep
        copy - the docstring's promise is only that the input is not mutated,
        which holds because migration touches no nested value. But a test that
        did not know this would write a comparison that cannot fail, so the
        aliasing is stated here where a future reader will find it.
        """
        lockfile = {
            "$schema": LOCKFILE_SCHEMA_URL,
            "schema": LOCKFILE_SCHEMA,
            "config_contract": SEMANTIC_CONFIG_VERSION,
            "config_digest": "a" * 64,
            "project": "p",
            "components": {"svc": {"fingerprints": {facet: None for facet in FACETS}}},
            "slices": {},
            "generated_at": "2020-01-01T00:00:00Z",
        }
        migrated = migrate_lockfile(lockfile)
        self.assertIs(migrated["components"], lockfile["components"])
        self.assertIs(
            migrated["components"]["svc"], lockfile["components"]["svc"]
        )
        self.assertIsNot(migrated, lockfile)

    def test_a_generated_lock_with_slices_survives_a_migration_that_rewrites(self):
        """A real lock, and a premise that migration actually did something."""
        with Scenario("migrate059") as scene:
            scene.component(
                "alpha", path="alpha", provider="path-hash",
                boundary=["api.txt"], behavior=["impl.py"],
                consumers=["beta"],
            )
            scene.component(
                "beta", path="beta", provider="json-canonical",
                boundary=["contract.json"],
            )
            scene.slice("everything", mode="exact", components=["alpha", "beta"])
            scene.file("alpha/api.txt", "api\n")
            scene.file("alpha/impl.py", "impl = 1\n")
            scene.json_file("beta/contract.json", {"name": "beta"})
            scene.commit()
            lock = generate_lockfile(scene.config, scene.root, source="working-tree")

        stamped = copy.deepcopy(lock)
        stamped["generated_at"] = "2020-01-01T00:00:00Z"
        # The premise: the stamped lock is one migration has to change, and the
        # change is exactly what makes it valid again.
        self.assertEqual(
            _lockfile_structure_issues(stamped),
            ["LOCKFILE malformed: unknown field in lockfile: generated_at"],
        )
        migrated = migrate_lockfile(stamped)
        self.assertEqual(_lockfile_structure_issues(migrated), [])
        self.assertNotIn("generated_at", migrated)
        self.assertIn("generated_at", stamped)
        self.assertEqual(migrated, lock)
        self.assertEqual(dump_lockfile(migrated), dump_lockfile(lock))
        # Named field by field, because dict equality alone reads as a claim
        # about nothing in particular.
        self.assertEqual(migrated["config_digest"], lock["config_digest"])
        self.assertEqual(
            migrated["slices"]["everything"]["fingerprint"],
            lock["slices"]["everything"]["fingerprint"],
        )
        self.assertEqual(
            migrated["slices"]["everything"]["component_digests"],
            lock["slices"]["everything"]["component_digests"],
        )
        self.assertTrue(
            any(
                digest is not None
                for entry in lock["components"].values()
                for digest in entry["fingerprints"].values()
            ),
            "the fixture recorded no fingerprints at all",
        )

    def test_the_locks_migration_refuses_are_the_ones_needing_repository_content(self):
        """Why the schema normalisation is a no-op for everything it accepts."""
        with Scenario("migrate059b") as scene:
            scene.component(
                "svc", path="svc", provider="path-hash", boundary=["api.txt"]
            )
            scene.file("svc/api.txt", "a\n")
            scene.commit()
            lock = generate_lockfile(scene.config, scene.root, source="working-tree")
        refusals = {
            "hash contract v1": ("schema", "boundary-lock/v1"),
            "hash contract v2": ("schema", "boundary-lock/v2"),
            "an unknown schema": ("schema", "boundary-lock/v9"),
            "an older semantic contract": (
                "config_contract",
                "boundver-semantic-config/v1",
            ),
        }
        for label, (field, value) in refusals.items():
            with self.subTest(refusal=label):
                probe = copy.deepcopy(lock)
                probe[field] = value
                with self.assertRaises(MigrationError):
                    migrate_lockfile(probe)
        missing = copy.deepcopy(lock)
        missing.pop("schema")
        with self.assertRaises(MigrationError):
            migrate_lockfile(missing)
        # So every lock that survives already carries the current schema, and
        # the relabelling step never has anything to relabel.
        self.assertEqual(lock["schema"], LOCKFILE_SCHEMA)
        self.assertEqual(migrate_lockfile(lock)["schema"], LOCKFILE_SCHEMA)


# ---------------------------------------------------------------------------
# OBL-HASHING-060: the persisted schema annotation does not follow the tag
# ---------------------------------------------------------------------------


def _load_release_readiness():
    """Import the release script the way tests/test_release_readiness.py does."""
    path = REPO_ROOT / "scripts" / "verify_release_readiness.py"
    spec = importlib.util.spec_from_file_location(
        "_chunk28_verify_release_readiness", path
    )
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SchemaAnnotationRatchetTests(unittest.TestCase):
    """The v4 schema keeps its v0.16.0 publication across patch upgrades."""

    @classmethod
    def setUpClass(cls):
        cls.readiness = _load_release_readiness()
        cls.lockfile_source = (
            REPO_ROOT / "src" / "boundver" / "_lockfile.py"
        ).read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("_chunk28_verify_release_readiness", None)

    def test_the_v4_schema_names_the_v0_16_0_publication(self):
        self.assertEqual(LOCKFILE_SCHEMA, "boundary-lock/v4")
        self.assertEqual(
            LOCKFILE_SCHEMA_URL,
            "https://raw.githubusercontent.com/yzm1/boundver/v0.16.0/"
            "spec/boundary.lock.schema.json",
        )

    def test_the_release_check_expects_the_ref_this_constant_carries(self):
        """The coupling: two constants in two files that must not drift apart."""
        match = self.readiness.RAW_SCHEMA_RE.fullmatch(LOCKFILE_SCHEMA_URL)
        self.assertIsNotNone(
            match, "the release check's own regex does not recognise this URL"
        )
        self.assertEqual(match.group("path"), "spec/boundary.lock.schema.json")
        self.assertEqual(match.group("ref"), self.readiness.CANONICAL_LOCK_SCHEMA_REF)

    def test_the_module_holding_the_constant_is_a_release_surface(self):
        """The premise: the file the check reads is one the check inventories."""
        selected = {
            path.relative_to(REPO_ROOT / "src").as_posix()
            for path in self.readiness._release_files(REPO_ROOT / "src")
        }
        self.assertIn("boundver/_lockfile.py", selected)

    def test_every_lock_schema_url_in_the_source_names_the_canonical_ref(self):
        refs = {
            match.group("ref")
            for match in self.readiness.RAW_SCHEMA_RE.finditer(self.lockfile_source)
            if match.group("path") == "spec/boundary.lock.schema.json"
        }
        self.assertEqual(refs, {self.readiness.CANONICAL_LOCK_SCHEMA_REF})

    def test_a_drifted_annotation_parses_to_a_ref_the_check_would_reject(self):
        """The premise: the comparison above can fail, given a drifted file."""
        drifted = self.lockfile_source.replace(
            self.readiness.CANONICAL_LOCK_SCHEMA_REF, "v9.9.9"
        )
        self.assertNotEqual(drifted, self.lockfile_source)
        refs = {
            match.group("ref")
            for match in self.readiness.RAW_SCHEMA_RE.finditer(drifted)
            if match.group("path") == "spec/boundary.lock.schema.json"
        }
        self.assertEqual(refs, {"v9.9.9"})
        self.assertNotIn(self.readiness.CANONICAL_LOCK_SCHEMA_REF, refs)

    def test_regenerating_under_a_newer_package_version_is_byte_identical(self):
        with Scenario("version060") as scene:
            scene.component(
                "svc", path="svc", provider="path-hash",
                boundary=["api.txt"], consumers=["ui"],
            )
            scene.component("ui", path="ui", provider="leaf", boundary=[])
            scene.slice("everything", mode="exact", components=["svc", "ui"])
            scene.file("svc/api.txt", "api\n")
            scene.file("ui/main.py", "u\n")
            scene.commit()
            lock_path = scene.root / "boundary.lock.json"

            renderings = {}
            reported = {}
            for version in ("0.15.0", "99.99.99"):
                with mock.patch.object(core, "_get_version", return_value=version):
                    reported[version] = run_cli_in_process(
                        scene.root, "--version"
                    ).stdout.strip()
                    result = run_cli_in_process(
                        scene.root, "generate", "--source", "working-tree"
                    )
                self.assertEqual(result.returncode, 0, result.stderr)
                renderings[version] = lock_path.read_bytes()
                lock_path.unlink()

            # The premise: the bumped version really reached the running CLI,
            # so "identical output" is a fact about two package versions and
            # not about one patch that never applied.
            self.assertEqual(
                reported, {"0.15.0": "boundver 0.15.0", "99.99.99": "boundver 99.99.99"}
            )
            self.assertEqual(renderings["0.15.0"], renderings["99.99.99"])
            self.assertNotIn(b"99.99.99", renderings["99.99.99"])
            self.assertIn(LOCKFILE_SCHEMA_URL.encode("utf-8"), renderings["0.15.0"])


# ---------------------------------------------------------------------------
# OBL-HASHING-061: dump and parse agree on everything generation can write
# ---------------------------------------------------------------------------

MODULE_NAME = "_chunk28_metadata_provider"


class MetadataProvider:
    """A provider whose only job is to put a nested object in the lock."""

    name = "custom.chunk28_metadata"
    version = "1"

    def resolve(self, ctx):
        return ResolvedBoundary(
            entries=[("canonical:contract", b"stable")],
            metadata={
                "operations": ["GET /ping", "POST /café"],
                "nested": {"depth": {"leaf": "éèê 日本語"}},
                "count": 3,
                "absent": None,
            },
        )


#: Provider metadata a round trip could not survive, each with the exact
#: refusal generation answers with. These are the premise for stating the
#: round-trip property over "every lock generate_lockfile can produce": the
#: values that would break it never reach a lock.
HOSTILE_METADATA = {
    "a non-finite number": (
        {"value": float("nan")},
        "metadata contains a non-finite number",
    ),
    "an infinity": (
        {"value": float("inf")},
        "metadata contains a non-finite number",
    ),
    "a lone surrogate": (
        {"value": "\ud800"},
        "metadata strings must contain valid Unicode",
    ),
    "a non-JSON scalar": (
        {"value": (1, 2)},
        "metadata contains non-JSON value tuple",
    ),
}


class LockSerialisationRoundTripTests(unittest.TestCase):
    """OBL-HASHING-061: boundver can read back everything it writes."""

    @classmethod
    def setUpClass(cls):
        cls.previous_module = sys.modules.get(MODULE_NAME)
        module = types.ModuleType(MODULE_NAME)
        module.MetadataProvider = MetadataProvider
        sys.modules[MODULE_NAME] = module

        cls.scene = Scenario("proyecto-ñ")
        scene = cls.scene
        cls.unicode_name = "servicio-café"
        cls.unicode_path = "componentes/café-ü"
        scene.component(
            cls.unicode_name, path=cls.unicode_path, provider="path-hash",
            boundary=["api-é.txt"],
        )
        scene.component("leaf_only", path="leaf_only", provider="leaf", boundary=[])
        scene.component(
            "implicit_only", path="implicit_only", provider="implicit", boundary=[]
        )
        scene.component(
            "meta", path="meta", provider=MetadataProvider.name,
            boundary=["contract.txt"],
        )
        scene.config["providers"] = [
            {"module": MODULE_NAME, "class": "MetadataProvider"}
        ]
        scene.slice(
            "todo", mode="exact", components=[cls.unicode_name, "leaf_only"]
        )
        scene.file(f"{cls.unicode_path}/api-é.txt", "contrato\n")
        scene.file("leaf_only/main.py", "x\n")
        scene.file("implicit_only/main.py", "y\n")
        scene.file("meta/contract.txt", "c\n")
        scene.commit()
        cls.lock = generate_lockfile(
            scene.config, scene.root, source="working-tree",
            allow_custom_providers=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()
        if cls.previous_module is None:
            sys.modules.pop(MODULE_NAME, None)
        else:  # pragma: no cover - only if a fixture name is ever reused
            sys.modules[MODULE_NAME] = cls.previous_module

    @given(lock_documents())
    @PROFILE
    def test_every_modelled_lock_survives_dump_and_reparse(self, lockfile):
        text = dump_lockfile(lockfile)
        self.assertEqual(parse_lockfile_bytes(text.encode("utf-8")), lockfile)

    def test_the_model_matches_the_shape_generation_emits(self):
        """The premise for the property: this model is not a private schema."""
        self.assertEqual(set(self.lock), set(LOCK_ROOT_KEYS))
        modelled_component_keys = (
            REQUIRED_COMPONENT_KEYS
            | set(OPTIONAL_ARRAY_KEYS)
            | {"boundary_metadata", "vendored_digests"}
        )
        for name, entry in self.lock["components"].items():
            with self.subTest(component=name):
                self.assertTrue(REQUIRED_COMPONENT_KEYS <= set(entry))
                self.assertTrue(set(entry) <= modelled_component_keys)
                self.assertEqual(set(entry["fingerprints"]), set(FACETS))
        for name, entry in self.lock["slices"].items():
            with self.subTest(slice=name):
                self.assertEqual(set(entry), set(SLICE_KEYS))

    def test_the_fixture_really_carries_the_four_awkward_cases(self):
        """The premise: absent arrays, null digests, unicode and metadata."""
        components = self.lock["components"]
        self.assertIn(self.unicode_name, components)
        self.assertEqual(components[self.unicode_name]["path"], self.unicode_path)
        self.assertTrue(any(ord(ch) > 127 for ch in self.unicode_name))
        self.assertIsNone(components["leaf_only"]["fingerprints"]["boundary"])
        self.assertIsNone(components["leaf_only"]["fingerprints"]["compat"])
        for field in OPTIONAL_ARRAY_KEYS:
            self.assertNotIn(field, components[self.unicode_name])
        self.assertIn("boundary_errors", components["implicit_only"])
        metadata = components["meta"]["boundary_metadata"]
        self.assertEqual(metadata["nested"]["depth"]["leaf"], "éèê 日本語")
        self.assertIsNone(metadata["absent"])
        self.assertEqual(_lockfile_structure_issues(self.lock), [])

    def test_the_generated_unicode_lock_round_trips_exactly(self):
        text = dump_lockfile(self.lock)
        # dump escapes non-ASCII, so the bytes on disk never carry the
        # character itself; parsing has to put it back.
        self.assertNotIn("café", text)
        self.assertIn("caf\\u00e9", text)
        parsed = parse_lockfile_bytes(text.encode("utf-8"))
        self.assertEqual(parsed, self.lock)
        self.assertEqual(
            parsed["components"][self.unicode_name]["path"], self.unicode_path
        )
        self.assertEqual(
            parsed["components"]["meta"]["boundary_metadata"],
            self.lock["components"]["meta"]["boundary_metadata"],
        )
        for field in OPTIONAL_ARRAY_KEYS:
            self.assertNotIn(field, parsed["components"][self.unicode_name])

    def test_metadata_that_would_break_the_round_trip_is_refused_at_generation(self):
        """The premise that makes "every lock generation can produce" true."""
        module = sys.modules[MODULE_NAME]
        original = module.MetadataProvider
        try:
            for label, (payload, expected) in HOSTILE_METADATA.items():
                with self.subTest(metadata=label):
                    module.MetadataProvider = _hostile_provider(payload)
                    with self.assertRaises(ConfigError) as raised:
                        generate_lockfile(
                            self.scene.config, self.scene.root,
                            source="working-tree", allow_custom_providers=True,
                        )
                    self.assertIn(expected, str(raised.exception))
        finally:
            module.MetadataProvider = original
        # And the premise for the premise: with the honest provider restored,
        # the same call succeeds, so the refusals were about the metadata.
        self.assertEqual(
            generate_lockfile(
                self.scene.config, self.scene.root, source="working-tree",
                allow_custom_providers=True,
            ),
            self.lock,
        )


def _hostile_provider(payload: dict):
    """A provider class returning *payload* as its boundary metadata."""

    class HostileProvider:
        name = MetadataProvider.name
        version = "1"

        def resolve(self, ctx):
            return ResolvedBoundary(
                entries=[("canonical:contract", b"stable")], metadata=dict(payload)
            )

    return HostileProvider


if __name__ == "__main__":  # pragma: no cover - convenience only
    unittest.main()
