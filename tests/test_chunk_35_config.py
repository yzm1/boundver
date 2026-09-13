"""Six configuration-layer promises whose witnesses had all been hand-written.

Every obligation in this file was already "tested" before it was written, and in
each case the existing test was a smaller claim wearing the same name. The
SemVer parser had a dozen chosen strings and no oracle, so a shared misreading
of the grammar - by the author of the parser and the author of its test - was
invisible; here the expectation comes from the regular expression published on
semver.org, extended only by the two departures boundver documents (an optional
leading `v`, and a two-component `1.2` whose patch defaults to `0`). The
baseline identity had round-trip tests written by the same build that produced
the identity, which cannot detect the failure the feature exists to prevent: a
reworded diagnostic string silently invalidating every customer's stored
baseline on upgrade. So the identities here are fifteen literal SHA-256 digests
recorded from a run of this build, together with the rendered issue text they
must still be recoverable from, and a companion test that recomputes them under
a changed domain string to prove the pins move when the encoding moves. The
consumer closure had one hand-drawn graph and no independent reference; here it
is diffed against a plain BFS over randomly generated graphs, and the
idempotence law is stated in the only form in which it is true - over the
seed-inclusive closure, because the default closure removes its own seeds and
so is deliberately not idempotent.

Two of the six turned out to be partly wrong about the code, which is worth more
than another green assertion. The register expected `--fail-fast` to diverge
from plain `verify` because `_lockfile`'s own safety-prefix tuple omits three
prefixes that `core._drift_exit_code` honours. It cannot: an AST walk over every
module in the package finds `Config unavailable` and `Verification error`
written only inside `_cmd_status`, which never asks for a limited report, and
the one `Config invalid` that `verify_lockfile` can produce is an early return
carrying a single issue in both modes. That test is in this file so the omission
becomes a failure the day someone emits one of those prefixes from the verify
path. The real divergence is elsewhere and had not been looked for: a lockfile
preflight failure is assembled in `core._cmd_verify` before `verify_lockfile` is
ever called, so `--fail-fast` reports the whole preflight list. The obligation's
"exactly one issue" is therefore false today, and is recorded as an
`expectedFailure` beside tests pinning the two-issue payload exactly.

The two enumerations are read at runtime rather than listed. The nine
terminating branches of `verify --format json` are recovered by parsing
`core.py` with `ast` for calls to `_print_verify_json`, and a spy installed over
that function records the caller's line number while nine repository states are
driven through the in-process CLI; a tenth branch added tomorrow fails the
census instead of being silently unvalidated. Getting there needed a fixture
that can reach the error paths on purpose: a behaviour selector that also covers
every boundary artifact (config validation rejects anything less), a version
file broken into non-JSON to make a gated facet unavailable rather than merely
drifted, and a `defaults.verify_facets` of `["boundary"]` with a behaviour-only
edit, which is the only cheap way to produce a run that is clean and has
observations at the same time.

Covers OBL-CONFIG-020, OBL-CONFIG-021, OBL-CONFIG-022, OBL-CONFIG-024,
OBL-CONFIG-025 and OBL-CONFIG-026.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import re
import sys
import unittest
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import boundver
from boundver import core
from boundver._baseline import (
    BASELINE_SCHEMA,
    _identity_entry,
    _issues_with_identities,
    apply_baseline,
    create_baseline,
    violation_identity,
)
from boundver._config import validate_config
from boundver._config_contract import component_identifier_problem
from boundver._consumer_graph import (
    affected_consumer_groups,
    consumer_closure,
    resolve_slice_components,
)
from boundver._review import _ReviewWorkBudget, _walk_consumer_graph
from boundver._utils import _bounded_diagnostic_text, _issue_facet, _short
from boundver.versions import parse_semver

from tests._parity import run_cli, run_cli_in_process
from tests._scenarios import Scenario

try:
    import jsonschema
except ImportError:  # pragma: no cover - the suite installs it
    jsonschema = None

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCHEMA_PATH = REPOSITORY_ROOT / "spec" / "cli-output.verify.schema.json"


# ---------------------------------------------------------------------------
# OBL-CONFIG-020: an oracle for the SemVer grammar
# ---------------------------------------------------------------------------

# The three production bodies of the regular expression published as the
# official SemVer 2.0.0 grammar on semver.org. Transcribed from the
# specification, not from src/boundver/versions.py, so a misreading of the
# grammar in the parser cannot be a misreading here as well.
_NUMERIC_IDENTIFIER = r"0|[1-9]\d*"
_PRERELEASE_IDENTIFIER = r"0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*"
_BUILD_METADATA = r"[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*"
_PRERELEASE = (
    rf"(?:-(?P<prerelease>(?:{_PRERELEASE_IDENTIFIER})"
    rf"(?:\.(?:{_PRERELEASE_IDENTIFIER}))*))?"
)
_BUILD = rf"(?:\+(?P<build>{_BUILD_METADATA}))?$"

#: The official three-component grammar.
OFFICIAL_SEMVER = re.compile(
    rf"^(?P<major>{_NUMERIC_IDENTIFIER})\.(?P<minor>{_NUMERIC_IDENTIFIER})"
    rf"\.(?P<patch>{_NUMERIC_IDENTIFIER})" + _PRERELEASE + _BUILD
)
#: boundver's documented two-component extension: the patch defaults to '0'.
TWO_COMPONENT_SEMVER = re.compile(
    rf"^(?P<major>{_NUMERIC_IDENTIFIER})\.(?P<minor>{_NUMERIC_IDENTIFIER})"
    + _PRERELEASE
    + _BUILD
)


def reference_semver(version: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (compat_family, api_surface, exact_version) from the grammar alone."""
    if not version:
        return (None, None, None)
    body = version[1:] if version.startswith("v") else version
    match = OFFICIAL_SEMVER.match(body)
    patch: Optional[str] = None
    if match is None:
        match = TWO_COMPONENT_SEMVER.match(body)
        patch = "0"
    if match is None:
        return (None, None, version)
    major, minor = match.group("major"), match.group("minor")
    if patch is None:
        patch = match.group("patch")
    return (major, f"{major}.{minor}", f"{major}.{minor}.{patch}")


#: Every spelling the obligation names by hand, plus the neighbours that make
#: each one mean something. Accept/reject and the parsed triple both matter.
SEMVER_CASES = (
    "1.2.3",
    "v1.2.3",
    "1.2",
    "v1.2",
    "0.0.4",
    "01.2.3",
    "1.02.3",
    "1.2.03",
    "1.2.3-0.1+a",
    "1.2.3+01",
    "1.2.3+0",
    "1.2.3-01",
    "1.2.3-0",
    "1.2.3-alpha..1",
    "1.2.3+.",
    "1.2.3-",
    "1.2.3+",
    "1.2.3+build!",
    "1.2.3-alpha+001",
    "1.2.3----R-S.12.9.1--.12+meta",
    "1.0.0+0.build.1-rc.10000aaa-kk-0.1",
    "1.2.3.4",
    "1",
    "v1",
    "v",
    "",
    "vv1.2.3",
    "V1.2.3",
    "1.2.3 ",
    " 1.2.3",
    "1.2.3-a+b+c",
    "1.2.3-a_b",
    "+1.2.3",
    "-1.2.3",
    "1.2.3-00a",
    "1.2.3-a00",
    "1.2.3+00a",
    "1.2-3",
    "1.2+build",
    "1.2-alpha",
)


def _leading_zero_tolerant_parse(version: str):
    """A deliberately wrong parser: the premise witness for the oracle."""
    body = version[1:] if version.startswith("v") else version
    parts = body.split(".")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        major, minor, patch = parts
        return (major, f"{major}.{minor}", f"{major}.{minor}.{patch}")
    return reference_semver(version)


@st.composite
def _grammar_versions(draw):
    """Draw from the shape of a version string rather than from characters."""
    prefix = draw(st.sampled_from(("", "v", "V", "vv")))
    core_numbers = draw(
        st.lists(
            st.text(alphabet="0123456789", min_size=1, max_size=3),
            min_size=1,
            max_size=4,
        )
    )
    identifier = st.text(alphabet="0123456789abZ-", min_size=0, max_size=4)
    parts = [prefix, ".".join(core_numbers)]
    if draw(st.booleans()):
        parts.append("-" + ".".join(draw(st.lists(identifier, min_size=1, max_size=3))))
    if draw(st.booleans()):
        parts.append("+" + ".".join(draw(st.lists(identifier, min_size=1, max_size=3))))
    return "".join(parts)


class SemVerReferenceAgreementTests(unittest.TestCase):
    """OBL-CONFIG-020: the hand-written parser against the published grammar."""

    def test_the_oracle_reports_a_parser_that_accepts_leading_zero_cores(self):
        """Premise: the differential below can fail, and this is what it sees."""
        divergences = [
            version
            for version in SEMVER_CASES
            if _leading_zero_tolerant_parse(version) != reference_semver(version)
        ]
        self.assertEqual(divergences, ["01.2.3", "1.02.3", "1.2.03"])

    def test_every_named_grammar_case_agrees_with_the_reference(self):
        for version in SEMVER_CASES:
            with self.subTest(version=version):
                self.assertEqual(parse_semver(version), reference_semver(version))

    @settings(max_examples=400, deadline=None, suppress_health_check=list(HealthCheck))
    @given(
        st.one_of(
            _grammar_versions(),
            st.text(alphabet="0123456789abcXYZ-._+v ", min_size=0, max_size=12),
        )
    )
    def test_generated_versions_agree_with_the_reference(self, version):
        self.assertEqual(parse_semver(version), reference_semver(version))

    def test_the_two_component_extension_defaults_the_patch_to_zero(self):
        """The one place boundver deliberately leaves the official grammar."""
        self.assertEqual(parse_semver("1.2"), ("1", "1.2", "1.2.0"))
        self.assertEqual(parse_semver("v1.2-rc.1"), ("1", "1.2", "1.2.0"))
        self.assertIsNone(OFFICIAL_SEMVER.match("1.2"))

    def test_a_build_identifier_may_have_a_leading_zero_but_a_prerelease_may_not(self):
        self.assertEqual(parse_semver("1.2.3+01"), ("1", "1.2", "1.2.3"))
        self.assertEqual(parse_semver("1.2.3-01"), (None, None, "1.2.3-01"))


# ---------------------------------------------------------------------------
# OBL-CONFIG-021: the baseline identity ratchet
# ---------------------------------------------------------------------------

#: (kind, subject, facet) -> the SHA-256 this build produces over
#: 'boundver-verify-baseline/v1\0kind\0subject\0facet'. Recorded from a run of
#: this build; a change to the domain string, the separator, the empty-facet
#: encoding, or the kind spelling moves every value here and invalidates every
#: baseline in the field.
IDENTITY_PINS = {
    ("component-facet", "svc", "exact"):
        "d5c01f58ab9bc4c056048b7442a0f39d3aa3a1bc9d6b7c71015c4cf4d37fd514",
    ("component-facet", "svc", "behavior"):
        "922a6924b6bdb7d31b0df5b2237be244154ed6c8e19fba1ea97248f0221e6ed3",
    ("component-facet", "svc", "boundary"):
        "4ffc53bf97bf1682454e2842da91ac3db72be7efcca379a3b6b88d47978b3a0c",
    ("component-facet", "svc", "compat"):
        "0ec6d80319befcf39703ef94af29644e370851aca2479730a36fafcce6c7a4c1",
    ("component-facet", "team: svc", "boundary"):
        "75386feceea23c6f7e64396d0ed7744d45c40f9a2b68526108444b298f86a222",
    ("component-facet", "svc\nnext", "compat"):
        "3f09574887dcc59bf33b50310f56eff6fcabfd15b3dbf3b1c52d3dfd2d2f0022",
    ("slice-facet", "core", "exact"):
        "843f89f2edbe46c9203c840cec276716029c0c82433cf9f2b03f2bdeb6ae22fd",
    ("slice-facet", "core", "behavior"):
        "a27a42212d8d041ae3e607a5a1c03fc06ece507563f6c5533e90238ff67ddb1f",
    ("slice-facet", "core", "boundary"):
        "55a64005e1c1bad7b4489b98a7e077b20eaa3e279a4cb14896172067766b8920",
    ("slice-facet", "core", "compat"):
        "5727cd4885950e9cb7bf44affc9c854461852303a5ba578e424ff108e5ab3340",
    ("slice-facet", "team: core", "boundary"):
        "1f28af225e4e7ba9e3f099f3baf01be7a4a1618840c9042d031ba5c9347a8243",
    ("affected-consumers", "svc", "direct"):
        "7a23739f2305b947cf522fdc3ba55999262497bb0cf5c56c9db88dce8baa935b",
    ("affected-consumers", "svc", "transitive"):
        "26eb96ba59aa6c5cf214956c7197a18a33e7a6ac80f7b91df9677dc42a783cd6",
    ("affected-consumers", "team: svc", "direct"):
        "75424f857704749a360c9ffafd043801a1bfbd5004cadcc347e4c3f346f3af32",
    ("affected-consumers", "team: svc", "transitive"):
        "f7246499767bc00e68b7f32707a5c2c0d6b8f31ef34105bed7d8d1aea1dc9531",
}

#: The rendered diagnostic a released boundver wrote -> the identity a stored
#: baseline holds for it. This is the half of the contract a documentation
#: rewording breaks: the strings are parsed back with a regular expression.
ISSUE_PINS = (
    (
        "MISMATCH svc.compat: lockfile=2b76e797e89b... current=de584044795b...",
        ("component-facet", "svc", "compat"),
    ),
    (
        "MISMATCH team: svc.boundary: lockfile=none current=16f3cc4f2d5f...",
        ("component-facet", "team: svc", "boundary"),
    ),
    (
        "MISMATCH svc\nnext.compat: lockfile=none current=aaaaaaaaaaaa...",
        ("component-facet", "svc\nnext", "compat"),
    ),
    (
        "SLICE MISMATCH core.exact: lockfile=none current=cccccccccccc...",
        ("slice-facet", "core", "exact"),
    ),
)

#: An issue set whose consumer diagnostics must bind to the component above
#: them, including one whose name contains the ': ' the body is split on.
CONSUMER_ISSUE_PINS = (
    (
        [
            "MISMATCH svc.compat: lockfile=2b76e797e89b... current=de584044795b...",
            "AFFECTED CONSUMERS svc: partner, web",
        ],
        ("affected-consumers", "svc", "direct"),
    ),
    (
        [
            "MISMATCH team: svc.boundary: lockfile=none current=16f3cc4f2d5f...",
            "AFFECTED CONSUMERS (TRANSITIVE) team: svc: partner",
        ],
        ("affected-consumers", "team: svc", "transitive"),
    ),
)

#: A compat mismatch and the two ancillary metadata diagnostics it covers.
COMPAT_CONTEXT = {
    "project": "scenario",
    "lock_schema": "boundary-lock/v3",
    "lock_digest": "a" * 64,
    "config_contract": "semantic-config/v3",
    "source": "head",
    "components_filter": [],
    "facets": None,
    "transitive": False,
    "policy_digest": "b" * 64,
}
COMPAT_ISSUES = [
    "MISMATCH svc.compat: lockfile=2b76e797e89b... current=de584044795b...",
    "METADATA MISMATCH svc.version: lockfile='1.2.3' current='2.0.0'",
    "METADATA MISMATCH svc.semver: lockfile={'compat_family': '1'} "
    "current={'compat_family': '2'}",
]


class BaselineIdentityRatchetTests(unittest.TestCase):
    """OBL-CONFIG-021: a field baseline must survive an upgrade of this build."""

    def test_every_pinned_identity_still_hashes_to_its_recorded_value(self):
        for (kind, subject, facet), identity in IDENTITY_PINS.items():
            with self.subTest(kind=kind, subject=subject, facet=facet):
                entry = _identity_entry(kind, subject, facet)
                self.assertEqual(entry["id"], identity)
                self.assertEqual(
                    (entry["kind"], entry["subject"], entry["facet"]),
                    (kind, subject, facet),
                )

    def test_every_kind_and_facet_the_encoder_accepts_is_pinned(self):
        """The pins must cover the surface, not a sample of it."""
        surface = {
            (kind, facet)
            for kind, facets in (
                ("component-facet", ("exact", "behavior", "boundary", "compat")),
                ("slice-facet", ("exact", "behavior", "boundary", "compat")),
                ("affected-consumers", ("direct", "transitive")),
            )
            for facet in facets
        }
        pinned = {(kind, facet) for kind, _subject, facet in IDENTITY_PINS}
        self.assertEqual(surface - pinned, set())

    def test_rendered_issue_text_still_reparses_to_its_pinned_identity(self):
        for issue, key in ISSUE_PINS:
            with self.subTest(issue=issue):
                identity = violation_identity(issue)
                self.assertIsNotNone(identity)
                self.assertEqual(
                    (identity["kind"], identity["subject"], identity["facet"]),
                    key,
                )
                self.assertEqual(identity["id"], IDENTITY_PINS[key])

    def test_consumer_diagnostics_still_reparse_to_their_pinned_identity(self):
        for issues, key in CONSUMER_ISSUE_PINS:
            with self.subTest(issues=issues):
                identified = _issues_with_identities(issues)
                identity = identified[-1][1]
                self.assertIsNotNone(identity)
                self.assertEqual(
                    (identity["kind"], identity["subject"], identity["facet"]),
                    key,
                )
                self.assertEqual(identity["id"], IDENTITY_PINS[key])

    def test_changing_the_encoding_moves_every_pinned_identity(self):
        """Premise: the pins are sensitive to exactly the changes they guard.

        Recomputing each identity with a different domain string, a different
        separator, and a different empty-facet encoding must move all fifteen.
        If any variant reproduced a pinned value the table would be proving
        nothing about those three choices.
        """
        variants = {
            "domain": lambda k, s, f: "\0".join(
                ("boundver-verify-baseline/v2", k, s, f)
            ),
            "separator": lambda k, s, f: "|".join((BASELINE_SCHEMA, k, s, f)),
            "kind-dropped": lambda k, s, f: "\0".join((BASELINE_SCHEMA, s, f)),
        }
        pinned_values = set(IDENTITY_PINS.values())
        for label, encode in variants.items():
            for (kind, subject, facet), identity in IDENTITY_PINS.items():
                with self.subTest(variant=label, subject=subject, facet=facet):
                    moved = hashlib.sha256(
                        encode(kind, subject, facet).encode("utf-8")
                    ).hexdigest()
                    self.assertNotEqual(moved, identity)
                    self.assertNotIn(moved, pinned_values)

    def test_compat_metadata_diagnostics_are_covered_by_the_compat_identity(self):
        baseline = create_baseline(COMPAT_CONTEXT, COMPAT_ISSUES)
        self.assertEqual(
            [entry["id"] for entry in baseline["violations"]],
            [IDENTITY_PINS[("component-facet", "svc", "compat")]],
        )
        new, acknowledged, stale = apply_baseline(
            baseline, COMPAT_CONTEXT, COMPAT_ISSUES
        )
        self.assertEqual(new, [])
        self.assertEqual(acknowledged, COMPAT_ISSUES)
        self.assertEqual(stale, [])

    def test_compat_metadata_alone_is_not_covered_without_its_mismatch(self):
        """Premise: the coverage rule above is a rule, not an unconditional pass."""
        baseline = create_baseline(COMPAT_CONTEXT, COMPAT_ISSUES)
        without_mismatch = COMPAT_ISSUES[1:]
        new, acknowledged, stale = apply_baseline(
            baseline, COMPAT_CONTEXT, without_mismatch
        )
        self.assertEqual(new, without_mismatch)
        self.assertEqual(acknowledged, [])
        self.assertEqual(
            stale, [IDENTITY_PINS[("component-facet", "svc", "compat")]]
        )


# ---------------------------------------------------------------------------
# OBL-CONFIG-022: the consumer closure against a plain BFS
# ---------------------------------------------------------------------------

def reference_reachable(edges: Dict[str, List[str]], seed: str) -> Set[str]:
    """Breadth-first reachability over declared edges, seed included.

    Deliberately written the naive way, with no reference to
    `_consumer_graph`: a queue, a visited set, and an edge filter that keeps
    only names that are themselves configured components.
    """
    seen = {seed}
    pending = deque([seed])
    while pending:
        node = pending.popleft()
        for successor in edges.get(node, ()):
            if successor in edges and successor not in seen:
                seen.add(successor)
                pending.append(successor)
    return seen


#: Named topologies the register lists as untested. Each maps a component name
#: to its declared consumers; the closure of "a" is checked against the BFS.
TOPOLOGIES = {
    "self-edge": {"a": ["a", "b"], "b": []},
    "two-cycle": {"a": ["b"], "b": ["a"]},
    "long-cycle": {"a": ["b"], "b": ["c"], "c": ["d"], "d": ["a"]},
    "diamond": {"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": []},
    "disconnected-island": {"a": ["b"], "b": [], "island": ["other"], "other": []},
    "duplicated-entries": {"a": ["b", "b", "b"], "b": ["c", "c"], "c": []},
    "unknown-consumer": {"a": ["b", "ghost"], "b": []},
    "self-edge-in-a-cycle": {"a": ["a", "b"], "b": ["b", "a"]},
    "single-node": {"a": []},
}

_GRAPH_NAMES = ("a", "b", "c", "d", "e")


@st.composite
def _random_graphs(draw):
    members = draw(
        st.lists(st.sampled_from(_GRAPH_NAMES), min_size=1, max_size=5, unique=True)
    )
    components = {}
    for name in members:
        components[name] = {
            "consumers": draw(
                st.lists(
                    st.sampled_from(_GRAPH_NAMES + ("ghost",)),
                    min_size=0,
                    max_size=6,
                )
            ),
            "external_consumers": draw(
                st.lists(st.sampled_from(("ext1", "ext2")), min_size=0, max_size=3)
            ),
        }
    return components


def _as_components(edges: Dict[str, List[str]]) -> Dict[str, dict]:
    return {name: {"consumers": list(targets)} for name, targets in edges.items()}


class ConsumerClosureTests(unittest.TestCase):
    """OBL-CONFIG-022: the set that tells a team what to re-verify."""

    def test_the_bfs_oracle_reports_a_closure_that_drops_one_hop(self):
        """Premise: the comparison below can fail, and this is what it sees."""
        edges = TOPOLOGIES["diamond"]
        components = _as_components(edges)
        one_hop_only = sorted(
            name
            for name in components["a"]["consumers"]
            if name in components
        )
        reference = sorted(reference_reachable(edges, "a") - {"a"})
        self.assertNotEqual(one_hop_only, reference)
        self.assertEqual(consumer_closure(components, ["a"]), reference)

    def test_every_named_topology_matches_the_reference_closure(self):
        for label, edges in TOPOLOGIES.items():
            with self.subTest(topology=label):
                components = _as_components(edges)
                closure = consumer_closure(components, ["a"])
                self.assertEqual(
                    closure, sorted(reference_reachable(edges, "a") - {"a"})
                )
                self.assertEqual(closure, sorted(set(closure)))
                seeded = consumer_closure(components, ["a"], include_seeds=True)
                self.assertEqual(seeded, sorted(reference_reachable(edges, "a")))

    @settings(max_examples=300, deadline=None, suppress_health_check=list(HealthCheck))
    @given(_random_graphs(), st.sampled_from(_GRAPH_NAMES))
    def test_random_graphs_match_the_reference_closure(self, components, seed):
        edges = {name: entry["consumers"] for name, entry in components.items()}
        expected = (
            sorted(reference_reachable(edges, seed) - {seed})
            if seed in components
            else []
        )
        closure = consumer_closure(components, [seed])
        self.assertEqual(closure, expected)
        self.assertEqual(closure, sorted(set(closure)))

    @settings(max_examples=300, deadline=None, suppress_health_check=list(HealthCheck))
    @given(_random_graphs(), st.sampled_from(_GRAPH_NAMES))
    def test_the_seeded_closure_is_idempotent(self, components, seed):
        """closure(closure(S)) == closure(S), over the seed-inclusive form.

        The default closure removes its own seeds, so it is not idempotent by
        construction; stating the law over the seed-inclusive form is the only
        way it is true, and the only way it is worth asserting.
        """
        once = consumer_closure(components, [seed], include_seeds=True)
        twice = consumer_closure(components, once, include_seeds=True)
        self.assertEqual(once, twice)

    @settings(max_examples=200, deadline=None, suppress_health_check=list(HealthCheck))
    @given(_random_graphs(), st.sampled_from(_GRAPH_NAMES))
    def test_transitive_impact_reports_the_closure_and_its_terminals(
        self, components, seed
    ):
        if seed not in components:
            return
        edges = {name: entry["consumers"] for name, entry in components.items()}
        reached = reference_reachable(edges, seed)
        expected_internal = sorted(reached - {seed})
        expected_external = sorted(
            {
                terminal
                for name in reached
                for terminal in components[name]["external_consumers"]
            }
        )
        groups = affected_consumer_groups(components, seed, transitive=True)
        self.assertEqual(groups["components"], expected_internal)
        self.assertEqual(groups["external_consumers"], expected_external)
        internal, external, _edges = _walk_consumer_graph(
            components, seed, transitive=True, budget=_ReviewWorkBudget()
        )
        self.assertEqual(sorted(internal), expected_internal)
        self.assertEqual(sorted(external), expected_external)

    @settings(max_examples=200, deadline=None, suppress_health_check=list(HealthCheck))
    @given(_random_graphs(), st.sampled_from(_GRAPH_NAMES))
    def test_a_closure_slice_resolves_to_the_seed_inclusive_closure(
        self, components, seed
    ):
        expected = (
            consumer_closure(components, [seed], include_seeds=True)
            if seed in components
            else []
        )
        self.assertEqual(
            resolve_slice_components({"closure_of": seed}, components), expected
        )

    def test_config_validation_rejects_the_self_edge_and_duplicate_topologies(self):
        """A finding: two of the listed topologies cannot reach a real config.

        `consumer_closure` tolerates a self edge and duplicated consumer
        entries defensively, and the property tests above exercise both through
        the API. Through a configuration file they are unreachable, because
        validation rejects them first - which is where the real protection is,
        so it is pinned here rather than assumed.
        """
        with Scenario() as scene:
            scene.component(
                "a",
                path="a",
                provider="path-hash",
                boundary=["*.py"],
                behavior=["*.py"],
                consumers=["a", "b", "b"],
            )
            scene.component(
                "b", path="b", provider="path-hash", boundary=["*.py"],
                behavior=["*.py"],
            )
            scene.file("a/m.py", "x = 1\n")
            scene.file("b/m.py", "x = 1\n")
            scene.commit()
            errors = validate_config(scene.config, scene.root, source="head")
        self.assertIn("Component 'a' cannot consume its own boundary", errors)
        self.assertIn("Component 'a' field 'consumers' contains duplicates", errors)

    def test_a_persisted_closure_slice_equals_the_recomputed_closure(self):
        with _closure_repository() as scene:
            self.assertEqual(run_cli(scene.root, "generate").returncode, 0)
            scene.commit("lock")
            lockfile = json.loads(
                (scene.root / "boundary.lock.json").read_text(encoding="utf-8")
            )
            persisted = lockfile["slices"]["downstream"]["components"]
        edges = {
            name: entry.get("consumers", [])
            for name, entry in scene.config["components"].items()
        }
        self.assertEqual(persisted, sorted(reference_reachable(edges, "a")))
        self.assertNotIn("island", persisted)

    def test_verify_and_why_report_the_same_transitive_closure(self):
        with _closure_repository() as scene:
            self.assertEqual(run_cli(scene.root, "generate").returncode, 0)
            scene.commit("lock")
            scene.file("a/m.py", "x = 2\n")
            scene.commit("drift the seed")
            verify = json.loads(
                run_cli(
                    scene.root, "verify", "--format", "json", "--transitive"
                ).stdout
            )
            why = json.loads(
                run_cli(
                    scene.root, "why", "a", "--format", "json", "--transitive"
                ).stdout
            )
            direct = json.loads(
                run_cli(scene.root, "why", "a", "--format", "json").stdout
            )
        edges = {
            name: entry.get("consumers", [])
            for name, entry in scene.config["components"].items()
        }
        reached = reference_reachable(edges, "a")
        expected_internal = sorted(reached - {"a"})
        expected_external = sorted(
            {
                terminal
                for name in reached
                for terminal in scene.config["components"][name].get(
                    "external_consumers", []
                )
            }
        )
        self.assertEqual(expected_internal, ["b", "c", "d", "e"])
        self.assertEqual(expected_external, ["p1", "p2"])
        impact = verify["consumer_impact"]
        self.assertEqual(len(impact), 1)
        self.assertEqual(impact[0]["component"], "a")
        self.assertTrue(impact[0]["transitive"])
        self.assertEqual(impact[0]["components"], expected_internal)
        self.assertEqual(impact[0]["external_consumers"], expected_external)
        self.assertEqual(why["affected_components"], expected_internal)
        self.assertEqual(why["affected_external_consumers"], expected_external)
        self.assertEqual(
            why["affected_consumers"], sorted(expected_internal + expected_external)
        )
        # Premise for the equality above: direct impact is a strictly smaller
        # answer on this graph, so agreement is not agreement on everything.
        self.assertEqual(direct["affected_components"], ["b", "c"])


def _closure_repository() -> Scenario:
    """A diamond with a two-cycle tail, external terminals, and an island."""
    scene = Scenario()
    graph = {
        "a": (["b", "c"], []),
        "b": (["d"], ["p1"]),
        "c": (["d"], []),
        "d": (["e"], ["p2"]),
        "e": (["d"], []),
        "island": ([], []),
    }
    for name, (consumers, externals) in graph.items():
        scene.component(
            name,
            path=name,
            provider="path-hash",
            boundary=["*.py"],
            behavior=["*.py"],
            consumers=consumers,
            external_consumers=externals,
        )
        scene.file(f"{name}/m.py", "x = 1\n")
    scene.slice("downstream", mode="boundary", closure_of="a")
    scene.commit()
    return scene


# ---------------------------------------------------------------------------
# Shared repository fixtures for the CLI-level obligations
# ---------------------------------------------------------------------------

def _declare(scene: Scenario) -> None:
    """One component with every facet available, plus a gated slice.

    `behavior` has to cover every boundary artifact or config validation
    refuses the declaration, which is why `api/*.yaml` appears in both lists.
    """
    scene.component(
        "svc",
        path="svc",
        provider="path-hash",
        boundary=["api/*.yaml"],
        behavior=["impl/*.py", "api/*.yaml"],
        version_source={"file": "version.json", "field": "version"},
        consumers=["web"],
        external_consumers=["partner"],
    )
    scene.component(
        "web", path="web", provider="path-hash", boundary=["*.py"], behavior=["*.py"]
    )
    scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
    scene.file("svc/impl/main.py", "x = 1\n")
    scene.json_file("svc/version.json", {"version": "1.2.3"})
    scene.file("web/app.py", "y = 1\n")
    scene.slice("core", mode="boundary", components=["svc", "web"])
    scene.commit()


def _locked_repository() -> Scenario:
    """A repository whose committed lock matches its committed tree."""
    scene = Scenario()
    _declare(scene)
    result = run_cli(scene.root, "generate")
    if result.returncode != 0:  # pragma: no cover - fixture guard
        scene.close()
        raise AssertionError(f"fixture generate failed: {result.stderr}")
    scene.git("add", "--all")
    scene.git("commit", "-m", "lock")
    return scene


def _drift_boundary(scene: Scenario) -> None:
    scene.file("svc/api/v1.yaml", "openapi: 3.1.0\ninfo: {}\n")
    scene.commit("boundary drift")


def _drift_compat(scene: Scenario) -> None:
    scene.json_file("svc/version.json", {"version": "2.0.0"})
    scene.commit("major bump")


def _drift_exact_only(scene: Scenario) -> None:
    scene.file("svc/impl/notes.txt", "hello\n")
    scene.commit("untracked-by-any-selector file")


def _break_version_source(scene: Scenario) -> None:
    scene.file("svc/version.json", "{ not json\n")
    scene.commit("unparseable version file")


def _two_versioned_components() -> Scenario:
    """Two components that both declare a version source, both locked.

    Needed for the safety-precedence clause: one component has to fail digest
    computation while another produces a genuine compat mismatch, so the full
    issue set carries both an integrity failure and the highest facet severity.
    """
    scene = Scenario()
    for name in ("svc", "web"):
        scene.component(
            name,
            path=name,
            provider="path-hash",
            boundary=["*.py"],
            behavior=["*.py"],
            version_source={"file": "version.json", "field": "version"},
        )
        scene.file(f"{name}/m.py", "x = 1\n")
        scene.json_file(f"{name}/version.json", {"version": "1.0.0"})
    scene.commit()
    result = run_cli(scene.root, "generate")
    if result.returncode != 0:  # pragma: no cover - fixture guard
        scene.close()
        raise AssertionError(f"fixture generate failed: {result.stderr}")
    scene.git("add", "--all")
    scene.git("commit", "-m", "lock")
    return scene


def _two_preflight_issues(scene: Scenario) -> None:
    scene.component(
        "extra", path="extra", provider="path-hash", boundary=["*.py"],
        behavior=["*.py"],
    )
    scene.file("extra/e.py", "z = 1\n")
    scene.config["project"] = "renamed"
    scene.commit("component set and project both differ")


# ---------------------------------------------------------------------------
# OBL-CONFIG-024: --fail-fast against plain verify
# ---------------------------------------------------------------------------

#: label -> (mutation applied to a locked repository, extra verify arguments,
#: the exit code plain verify produces). The exit code is pinned so a state
#: that stops reaching its intended branch fails here rather than degenerating
#: into a comparison of two zeroes.
FAIL_FAST_STATES: Dict[str, Tuple[Optional[Callable[[Scenario], None]], Tuple[str, ...], int]] = {
    "clean": (None, (), 0),
    "boundary-behaviour-exact-slice": (_drift_boundary, (), 4),
    "compat": (_drift_compat, (), 5),
    "exact-only": (_drift_exact_only, (), 1),
    "unavailable-facet": (_break_version_source, ("--facets", "compat"), 2),
    "preflight": (_two_preflight_issues, (), 2),
}


class FailFastDifferentialTests(unittest.TestCase):
    """OBL-CONFIG-024: the reduced report must not change the verdict."""

    observed: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def setUpClass(cls):
        cls.observed = {}
        for label, (mutate, extra, _expected) in FAIL_FAST_STATES.items():
            with _locked_repository() as scene:
                if mutate is not None:
                    mutate(scene)
                plain = run_cli(scene.root, "verify", "--format", "json", *extra)
                fast = run_cli(
                    scene.root, "verify", "--format", "json", "--fail-fast", *extra
                )
                cls.observed[label] = {
                    "plain_code": plain.returncode,
                    "fast_code": fast.returncode,
                    "plain": json.loads(plain.stdout) if plain.stdout.strip() else None,
                    "fast": json.loads(fast.stdout) if fast.stdout.strip() else None,
                }

    def test_the_state_table_produces_several_distinct_exit_codes(self):
        """Premise: the equality below is not an equality of six zeroes."""
        codes = {
            label: record["plain_code"] for label, record in self.observed.items()
        }
        self.assertEqual(
            codes,
            {
                label: expected
                for label, (_mutate, _extra, expected) in FAIL_FAST_STATES.items()
            },
        )
        self.assertEqual(len(set(codes.values())), 5)

    def test_fail_fast_reproduces_the_plain_exit_code_for_every_state(self):
        for label, record in self.observed.items():
            with self.subTest(state=label):
                self.assertEqual(record["fast_code"], record["plain_code"])

    def test_fail_fast_reduces_the_report_wherever_verify_lockfile_produced_it(self):
        for label, record in self.observed.items():
            with self.subTest(state=label):
                expected = 0 if label == "clean" else 1
                self.assertEqual(len(record["fast"]["issues"]), expected)

    def test_fail_fast_returns_exactly_one_issue_for_every_failing_state(self):
        """Every verification phase honors the one-issue report contract."""
        for label, record in self.observed.items():
            if record["plain_code"] == 0:
                continue
            with self.subTest(state=label):
                self.assertEqual(len(record["fast"]["issues"]), 1)

    def test_fail_fast_limits_but_does_not_weaken_preflight(self):
        record = self.observed["preflight"]
        self.assertEqual(
            record["plain"]["issues"],
            [
                "METADATA MISMATCH project: lockfile='scenario' current='renamed'",
                "LOCKFILE component set differs from config: "
                "locked=['svc', 'web'] configured=['extra', 'svc', 'web']",
            ],
        )
        self.assertEqual(record["fast"]["issues"], record["plain"]["issues"][:1])

    def test_fail_fast_limits_invalid_config_reports_too(self):
        with _locked_repository() as scene:
            scene.config["project"] = ""
            scene.config["components"] = {}
            scene.commit("two invalid config fields")
            plain_result = run_cli(scene.root, "verify", "--format", "json")
            fast_result = run_cli(
                scene.root,
                "verify",
                "--format",
                "json",
                "--fail-fast",
            )
        plain = json.loads(plain_result.stdout)
        fast = json.loads(fast_result.stdout)
        self.assertEqual(plain_result.returncode, 2)
        self.assertEqual(fast_result.returncode, plain_result.returncode)
        self.assertGreaterEqual(len(plain["issues"]), 2)
        self.assertEqual(fast["issues"], plain["issues"][:1])

    def test_a_safety_prefixed_issue_outranks_a_higher_severity_mismatch(self):
        """The reduced report must not hide an integrity failure behind compat."""
        with _two_versioned_components() as scene:
            scene.file("svc/version.json", "{ not json\n")
            scene.json_file("web/version.json", {"version": "2.0.0"})
            scene.commit("break one version source and bump the other")
            plain = json.loads(
                run_cli(
                    scene.root, "verify", "--format", "json", "--facets", "compat"
                ).stdout
            )
            fast = json.loads(
                run_cli(
                    scene.root, "verify", "--format", "json", "--facets", "compat",
                    "--fail-fast",
                ).stdout
            )
        self.assertIn(
            "UNAVAILABLE FACET svc.compat: selected gate requires both locked "
            "and current digests",
            plain["issues"],
        )
        # The full set carries a real compat mismatch, the numerically highest
        # facet severity there is, and the reduction must not return it.
        compat_mismatches = [
            issue
            for issue in plain["issues"]
            if issue.startswith("MISMATCH web.compat: ")
        ]
        self.assertEqual(len(compat_mismatches), 1)
        self.assertEqual(_issue_facet(compat_mismatches[0]), "compat")
        safety_issues = [
            issue
            for issue in plain["issues"]
            if issue.startswith("CURRENT DIGEST ERROR svc: ")
        ]
        self.assertEqual(len(safety_issues), 1)
        self.assertEqual(fast["issues"], safety_issues)
        self.assertIsNone(_issue_facet(fast["issues"][0]))
        # Premise: the reduction really did drop something, so keeping the
        # safety issue is a choice rather than the only option available.
        self.assertGreater(len(plain["issues"]), 1)

    def test_the_config_invalid_issue_verify_can_return_is_identical_in_both_modes(self):
        """The one safety-ish prefix `verify_lockfile` itself can emit."""
        with Scenario() as scene:
            scene.component(
                "svc", path="svc", provider="path-hash", boundary=["*.py"],
                behavior=["*.py"],
            )
            scene.file("svc/m.py", "x = 1\n")
            scene.commit()
            lockfile = scene.generate()
            scene.config.setdefault("slices", {})["empty"] = {
                "mode": "exact",
                "components": [],
            }
            plain = scene.verify(lockfile)
            fast = scene.verify(lockfile, fail_fast=True)
        expected = [
            "Config invalid: Slice 'empty' field 'components' must contain at "
            "least one configured component; add a component name or remove the "
            "empty slice"
        ]
        self.assertEqual(plain, expected)
        self.assertEqual(fast, expected)
        self.assertEqual(core._drift_exit_code(plain), core._drift_exit_code(fast))

    def test_core_classifies_the_three_prefixes_the_lockfile_tuple_omits(self):
        """Premise for the census below: the asymmetry the register found is real."""
        for prefix in ("Config invalid", "Config unavailable", "Verification error"):
            with self.subTest(prefix=prefix):
                self.assertEqual(
                    core._drift_exit_code([f"{prefix}: detail"]), core.EXIT_USAGE
                )
        self.assertEqual(
            core._drift_exit_code(["MISMATCH svc.compat: lockfile=a current=b"]),
            core.EXIT_COMPAT,
        )

    def test_the_two_status_only_safety_prefixes_are_written_only_by_status(self):
        """A stale premise, pinned so it fails the day it stops being stale.

        The register expected `--fail-fast` to misclassify because
        `_lockfile`'s own safety-prefix tuple omits `Config invalid`,
        `Config unavailable` and `Verification error` while
        `core._drift_exit_code` honours all three. Walking the AST of every
        module in the package shows the last two are written only inside
        `_cmd_status`, which never asks for a limited report, so the omission
        cannot be reached. The one `Config invalid` the verify path can write
        is the early return pinned above. If one of these is ever emitted from
        another function, this fails and the omission becomes live.
        """
        writers = _prefix_writers(("Config unavailable", "Verification error"))
        self.assertEqual(
            {(module, function) for module, function, _line in writers},
            {("core.py", "_cmd_status")},
        )
        invalid_writers = _prefix_writers(("Config invalid",))
        self.assertEqual(
            {(module, function) for module, function, _line in invalid_writers},
            {("core.py", "_cmd_status"), ("_lockfile.py", "verify_lockfile")},
        )


def _prefix_writers(prefixes: Tuple[str, ...]) -> Set[Tuple[str, str, int]]:
    """Every (module, function, line) building a diagnostic with one of *prefixes*.

    Only interpolated strings count. Each of these diagnostics appends a
    detail, so each is an f-string; the same literals also appear as plain
    constants inside the two safety-prefix tuples that classify them, and that
    is a read of the prefix rather than a write of a diagnostic.
    """
    package = Path(boundver.__file__).resolve().parent
    found: Set[Tuple[str, str, int]] = set()
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                head = _interpolated_head(node)
                if head is not None and head.startswith(prefixes):
                    found.add((path.name, function.name, node.lineno))
    return found


def _interpolated_head(node: ast.AST) -> Optional[str]:
    """The leading literal text of an f-string, or None for anything else."""
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


# ---------------------------------------------------------------------------
# OBL-CONFIG-025: every terminating branch of verify --format json
# ---------------------------------------------------------------------------

def _gate_boundary_only(scene: Scenario) -> None:
    """Leave the gated facet clean and drift two ungated ones.

    Gating only `boundary` and editing a behaviour-only file is the cheapest
    state that is simultaneously `ok` and carrying observations, which is the
    precondition for the two `--update` branches that report no issues.
    """
    scene.config["defaults"] = {"verify_facets": ["boundary"]}
    scene.commit("gate boundary only")
    run_cli(scene.root, "generate")
    scene.commit("relock under the new policy")
    scene.file("svc/impl/main.py", "x = 2\n")
    scene.commit("behaviour-only drift")


def _invalid_config(scene: Scenario) -> None:
    scene.config["components"]["svc"]["consumers"] = ["nope"]
    scene.commit("unknown consumer")


def _add_a_component(scene: Scenario) -> None:
    scene.component(
        "extra", path="extra", provider="path-hash", boundary=["*.py"],
        behavior=["*.py"],
    )
    scene.file("extra/e.py", "z = 1\n")
    scene.commit("component missing from the lock")


def _write_baseline_then_drift(scene: Scenario) -> None:
    _drift_boundary(scene)
    run_cli(scene.root, "verify", "--format", "json", "--write-baseline", "bv.json")
    scene.commit("store the reviewed baseline")


#: label -> (mutation, verify arguments). Between them these states must reach
#: every `_print_verify_json` call site `core.py` contains; the census below
#: enumerates the sites from the source rather than trusting this list.
VERIFY_JSON_STATES: Tuple[Tuple[str, Optional[Callable[[Scenario], None]], Tuple[str, ...]], ...] = (
    ("invalid-config", _invalid_config, ()),
    ("preflight-failure", _add_a_component, ()),
    ("preflight-repair-update", _add_a_component, ("--update", "--source", "working-tree")),
    ("drift", _drift_boundary, ()),
    ("drift-update", _drift_boundary, ("--update", "--source", "working-tree")),
    ("unavailable-facet-update", _break_version_source,
     ("--facets", "compat", "--update", "--source", "working-tree")),
    ("clean", None, ()),
    ("clean-observations", _gate_boundary_only, ()),
    ("clean-observations-update", _gate_boundary_only,
     ("--update", "--source", "working-tree")),
    ("baseline-created", _drift_boundary, ("--write-baseline", "bv.json")),
    ("baseline-applied", _write_baseline_then_drift, ("--baseline", "bv.json")),
    ("baseline-updated", _write_baseline_then_drift, ("--update-baseline", "bv.json")),
)

#: The conditional `allOf` rules of spec/cli-output.verify.schema.json, restated
#: as predicates over the payload so a document is checked twice: once by
#: jsonschema, once here. A schema whose `if` clause silently stopped matching
#: would keep validating everything; these cannot.
SCHEMA_IMPLICATIONS: Dict[str, Callable[[dict], bool]] = {
    "ok=true implies no issues":
        lambda d: not d["ok"] or d["issues"] == [],
    "ok=false implies updated=false":
        lambda d: d["ok"] or d["updated"] is False,
    "updated=false implies no resolved issues":
        lambda d: d["updated"] or d["resolved_issues"] == [],
    "baseline present implies updated=false":
        lambda d: "baseline" not in d or d["updated"] is False,
    "baseline applied implies no added or removed ids":
        lambda d: d.get("baseline", {}).get("action") != "applied"
        or (d["baseline"]["added_ids"] == [] and d["baseline"]["removed_ids"] == []),
    "baseline created implies ok, no issues, no removed or stale ids":
        lambda d: d.get("baseline", {}).get("action") != "created"
        or (
            d["ok"]
            and d["issues"] == []
            and d["baseline"]["removed_ids"] == []
            and d["baseline"]["stale_ids"] == []
        ),
    "baseline updated implies ok, no issues, no added or stale ids":
        lambda d: d.get("baseline", {}).get("action") != "updated"
        or (
            d["ok"]
            and d["issues"] == []
            and d["baseline"]["added_ids"] == []
            and d["baseline"]["stale_ids"] == []
        ),
}


def _print_verify_json_call_sites() -> List[int]:
    """The lines of core.py that call `_print_verify_json`, read from source."""
    source = Path(inspect.getsourcefile(core)).read_text(encoding="utf-8")
    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_print_verify_json"
    )


@unittest.skipIf(jsonschema is None, "jsonschema is unavailable")
class VerifyJsonBranchTests(unittest.TestCase):
    """OBL-CONFIG-025: the published integration contract on every exit path."""

    call_sites: List[int] = []
    payloads: Dict[str, dict] = {}
    reached: Dict[str, List[int]] = {}

    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(VERIFY_SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.call_sites = _print_verify_json_call_sites()
        cls.payloads = {}
        cls.reached = {}
        original = core._print_verify_json
        recorded: List[int] = []

        def spy(**kwargs):
            recorded.append(sys._getframe(1).f_lineno)
            return original(**kwargs)

        core._print_verify_json = spy
        try:
            for label, mutate, arguments in VERIFY_JSON_STATES:
                with _locked_repository() as scene:
                    if mutate is not None:
                        mutate(scene)
                    recorded.clear()
                    result = run_cli_in_process(
                        scene.root, "verify", "--format", "json", *arguments
                    )
                    cls.reached[label] = list(recorded)
                    cls.payloads[label] = (
                        json.loads(result.stdout) if result.stdout.strip() else None
                    )
        finally:
            core._print_verify_json = original

    def test_the_census_can_report_a_call_site_no_state_reached(self):
        """Premise: the comparison below is capable of failing."""
        single = set(self.reached["clean"])
        self.assertEqual(len(single), 1)
        self.assertNotEqual(single, set(self.call_sites))
        self.assertTrue(single.issubset(set(self.call_sites)))

    def test_every_print_verify_json_call_site_was_driven_by_a_state(self):
        reached = {line for lines in self.reached.values() for line in lines}
        self.assertEqual(reached, set(self.call_sites))

    def test_every_state_emitted_exactly_one_document(self):
        for label, lines in self.reached.items():
            with self.subTest(state=label):
                self.assertEqual(len(lines), 1)
                self.assertIsNotNone(self.payloads[label])

    def test_every_emitted_document_validates_against_the_published_schema(self):
        for label, payload in self.payloads.items():
            with self.subTest(state=label):
                jsonschema.validate(payload, self.schema)

    def test_every_emitted_document_satisfies_the_conditional_rules_directly(self):
        for label, payload in self.payloads.items():
            for rule, holds in SCHEMA_IMPLICATIONS.items():
                with self.subTest(state=label, rule=rule):
                    self.assertTrue(holds(payload), payload)

    def test_the_three_baseline_actions_were_all_observed(self):
        actions = {
            payload["baseline"]["action"]
            for payload in self.payloads.values()
            if "baseline" in payload
        }
        self.assertEqual(actions, {"applied", "created", "updated"})

    def test_the_error_branches_reported_their_issues_rather_than_ok(self):
        """The five branches the register named as never schema-validated."""
        for label in (
            "invalid-config",
            "preflight-failure",
            "unavailable-facet-update",
        ):
            with self.subTest(state=label):
                payload = self.payloads[label]
                self.assertFalse(payload["ok"])
                self.assertFalse(payload["updated"])
                self.assertTrue(payload["issues"])
        for label in ("preflight-repair-update", "clean-observations-update"):
            with self.subTest(state=label):
                payload = self.payloads[label]
                self.assertTrue(payload["ok"])
                self.assertTrue(payload["updated"])
                self.assertEqual(payload["issues"], [])
        self.assertEqual(self.payloads["preflight-repair-update"]["resolved_issues"],
                         [
                             "LOCKFILE component set differs from config: "
                             "locked=['svc', 'web'] configured=['extra', 'svc', 'web']"
                         ])
        self.assertEqual(self.payloads["clean-observations-update"]["resolved_issues"], [])
        self.assertTrue(self.payloads["clean-observations-update"]["observations"])

    def test_the_schema_rejects_a_document_that_breaks_a_conditional_rule(self):
        """Premise: validation is live, not a no-op on a permissive schema."""
        clean = self.payloads["clean"]
        broken_required = dict(clean)
        broken_required.pop("consumer_impact")
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(broken_required, self.schema)
        broken_conditional = dict(clean, issues=["MISMATCH svc.exact: a b"])
        self.assertTrue(broken_conditional["ok"])
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(broken_conditional, self.schema)
        applied = dict(self.payloads["baseline-applied"], updated=True)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(applied, self.schema)


# ---------------------------------------------------------------------------
# OBL-CONFIG-026: issue text back to (kind, subject, facet)
# ---------------------------------------------------------------------------

#: Names crafted against the greedy `(?P<subject>.+)\.(?P<facet>...)`: under
#: re.DOTALL. Each must reparse to itself, not to a shorter or longer subject.
ADVERSARIAL_NAMES = (
    "svc",
    "svc.exact",
    "svc.compat",
    "a.boundary: x",
    "MISMATCH y",
    "SLICE MISMATCH z",
    "AFFECTED CONSUMERS q",
    "a\nb",
    "team: svc",
    "svc.",
    ".exact",
    "x.behavior: y.compat: z",
    "1",
    "-",
)

FACETS = ("exact", "behavior", "boundary", "compat")

#: The exact renderings src/boundver/_lockfile.py:1441 and :1619 produce.
_COMPONENT_ISSUE = "MISMATCH {subject}.{facet}: lockfile={locked} current={current}"
_SLICE_ISSUE = "SLICE MISMATCH {subject}.{facet}: lockfile={locked} current={current}"

_NAME_PIECES = (
    "svc", "a", ".", ":", " ", "exact", "behavior", "boundary", "compat",
    "MISMATCH ", "SLICE MISMATCH ", "AFFECTED CONSUMERS ", "\n", "\0",
    ".exact:", "-", "0", "é",
)


def _render(kind: str, subject: str, facet: str, locked, current) -> str:
    template = _COMPONENT_ISSUE if kind == "component-facet" else _SLICE_ISSUE
    return template.format(
        subject=subject, facet=facet, locked=_short(locked), current=_short(current)
    )


class IssueTextRoundTripTests(unittest.TestCase):
    """OBL-CONFIG-026: identities are recovered from a rendered display string."""

    def test_a_mis_split_rendering_is_reported_by_the_round_trip_check(self):
        """Premise: the round-trip assertion below can fail, and here is how."""
        mangled = "MISMATCH svc.exact.boundary: lockfile=none current=none"
        identity = violation_identity(mangled)
        self.assertIsNotNone(identity)
        self.assertEqual(identity["subject"], "svc.exact")
        self.assertEqual(identity["facet"], "boundary")
        self.assertNotEqual(
            identity["id"], IDENTITY_PINS[("component-facet", "svc", "exact")]
        )

    def test_every_adversarial_name_round_trips_for_every_facet(self):
        for name in ADVERSARIAL_NAMES:
            self.assertIsNone(
                component_identifier_problem(name),
                f"fixture error: {name!r} is not a legal component name",
            )
            for kind in ("component-facet", "slice-facet"):
                for facet in FACETS:
                    with self.subTest(name=name, kind=kind, facet=facet):
                        issue = _render(kind, name, facet, None, "f" * 64)
                        identity = violation_identity(issue)
                        self.assertIsNotNone(identity, issue)
                        self.assertEqual(
                            (
                                identity["kind"],
                                identity["subject"],
                                identity["facet"],
                            ),
                            (kind, name, facet),
                        )
                        self.assertEqual(
                            identity["id"], _identity_entry(kind, name, facet)["id"]
                        )

    @settings(max_examples=500, deadline=None, suppress_health_check=list(HealthCheck))
    @given(
        st.lists(st.sampled_from(_NAME_PIECES), min_size=1, max_size=6).map("".join),
        st.sampled_from(FACETS),
        st.sampled_from(("component-facet", "slice-facet")),
        st.sampled_from((None, "a" * 64, "0123456789abcdef" * 4)),
        st.sampled_from((None, "b" * 64)),
    )
    def test_generated_names_round_trip_through_the_rendered_issue(
        self, name, facet, kind, locked, current
    ):
        if component_identifier_problem(name) is not None:
            return
        display = _bounded_diagnostic_text(name)
        issue = _render(kind, display, facet, locked, current)
        identity = violation_identity(issue)
        self.assertIsNotNone(identity, issue)
        self.assertEqual(
            (identity["kind"], identity["subject"], identity["facet"]),
            (kind, display, facet),
        )

    @settings(max_examples=300, deadline=None, suppress_health_check=list(HealthCheck))
    @given(
        st.lists(
            st.tuples(
                st.sampled_from(
                    ("component-facet", "slice-facet", "affected-consumers")
                ),
                st.lists(st.sampled_from(_NAME_PIECES), min_size=1, max_size=4).map(
                    "".join
                ),
            ),
            min_size=2,
            max_size=8,
        )
    )
    def test_distinct_subject_and_facet_triples_never_share_an_identity(self, draws):
        seen: Dict[str, Tuple[str, str, str]] = {}
        for kind, subject in draws:
            if component_identifier_problem(subject) is not None:
                continue
            facets = (
                ("direct", "transitive")
                if kind == "affected-consumers"
                else FACETS
            )
            for facet in facets:
                key = (kind, subject, facet)
                identity = _identity_entry(*key)["id"]
                self.assertEqual(seen.setdefault(identity, key), key)

    def test_a_consumer_diagnostic_binds_to_the_component_above_it(self):
        """Even when the consumer list itself contains an embedded ': '."""
        issues = [
            "MISMATCH team: svc.boundary: lockfile=none current=aaaaaaaaaaaa...",
            "AFFECTED CONSUMERS team: svc: team: web, partner",
            "MISMATCH svc.compat: lockfile=none current=bbbbbbbbbbbb...",
            "AFFECTED CONSUMERS svc: web",
        ]
        identified = _issues_with_identities(issues)
        self.assertEqual(
            [
                (entry[1]["kind"], entry[1]["subject"], entry[1]["facet"])
                for entry in identified
            ],
            [
                ("component-facet", "team: svc", "boundary"),
                ("affected-consumers", "team: svc", "direct"),
                ("component-facet", "svc", "compat"),
                ("affected-consumers", "svc", "direct"),
            ],
        )

    def test_an_unowned_consumer_diagnostic_is_not_given_an_identity(self):
        """Premise: the binding above is a binding, not a match on anything."""
        identified = _issues_with_identities(["AFFECTED CONSUMERS svc: web"])
        self.assertIsNone(identified[0][1])
        self.assertIsNone(violation_identity("AFFECTED CONSUMERS svc: web"))

    def test_unavailable_and_metadata_diagnostics_stay_unbaselinable(self):
        for issue in (
            "UNAVAILABLE FACET svc.compat: selected gate requires both locked "
            "and current digests",
            "CURRENT DIGEST ERROR svc: Configured version source did not "
            "produce a version",
            "METADATA MISMATCH svc.version: lockfile='1.2.3' current='2.0.0'",
            "LOCKFILE component set differs from config: locked=[] configured=[]",
        ):
            with self.subTest(issue=issue):
                self.assertIsNone(violation_identity(issue))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
