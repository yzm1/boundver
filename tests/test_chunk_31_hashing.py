"""Five aggregation points where boundver decides that nothing changed.

A digest is only as good as the thing it is computed over, and the five
obligations answered here all live at a seam where one layer hands a summary to
another. A slice fingerprint summarises its members; the behavior envelope
summarises a boundary it is supposed to contain; a verification baseline
summarises the scope it was recorded under; a diff summarises two documents a
pull request supplied; and the custom-provider loader summarises a name it
checked four separate times. In each case the interesting failure is not a
wrong digest but a confident "unchanged" over an input the code never really
looked at.

Most of the checking here is derived rather than listed. The slice fingerprint
is compared against an independent recomputation built from `json.dumps` and
`hashlib` rather than from boundver's own `canonical_json`, which makes
generated adversarial component names - names carrying quotes, colons, braces
and backslashes - a real injection test instead of a second call into the
function under test. The baseline context axes are enumerated at runtime from
`baseline_context()` and each is mutated in turn, so an axis added tomorrow is
refused automatically, with a separate assertion pinning the exact nine names
so an axis quietly dropped fails instead of vanishing from the loop. The
behavior envelope is reconstructed from `_hash_framed_entries` over a boundary
digest that is itself observable in the lockfile: a component whose boundary
paths equal its behavior paths publishes the inner behavior digest as its own
`boundary` fingerprint, which is what makes the `none:{status}` fallback
checkable without reimplementing the envelope.

Three of the fixtures took real care. The three absent-boundary states are not
reachable through `generate`, because `_generation_errors` refuses to bless a
lock whose boundary errored, so the entries are computed through
`_compute_component_entry` against a live `_SourceAccessor`; `leaf` supplies
`none:ok`, `implicit` with no declared paths supplies `none:partial`, and a
provider whose `resolve` raises supplies `none:error`. The baseline sequence
has to commit the baseline file after writing it, because at `source=head` an
untracked baseline is not visible to the snapshot and the apply step exits 2
for a reason that has nothing to do with the ratchet. And the shape-shifting
provider is an instance-scoped counter rather than a class-scoped one, because
`load_custom_providers` constructs a fresh instance per entry and a class
counter would make the second entry in a two-entry config start mid-sequence.

Three divergences came out of it, all pinned rather than fixed. The slice
fingerprint does not bind the slice mode. `diff_lockfiles` reports two
different malformed members as `unchanged`. And `load_custom_providers` checks
one string and keys the registry with another, so a declared extension can take
over the `path-hash` name with no error at all.

Covers OBL-HASHING-079, OBL-HASHING-080, OBL-HASHING-081, OBL-HASHING-086 and
OBL-HASHING-108.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import sys
import types
import unittest
from typing import Any, Dict, List, Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from boundver._baseline import (
    BASELINE_SCHEMA,
    BaselineError,
    apply_baseline,
    baseline_context,
    create_baseline,
)
from boundver._diff import diff_lockfiles, require_compatible_lockfile_schemas
from boundver._hashing import HASH_DOMAIN_BEHAVIOR, _hash_framed_entries
from boundver._lockfile import (
    LOCKFILE_SCHEMA,
    SEMANTIC_CONFIG_VERSION,
    MigrationError,
    _capture_git_source_snapshot,
    _compute_component_entry,
    _recompute_slice_entry,
    _SourceAccessor,
    migrate_lockfile,
)
from boundver._utils import FACETS, LockfileError
from boundver.core import _require_diffable_lockfile
from boundver.providers import (
    ResolvedBoundary,
    create_registry,
    load_custom_providers,
)

from tests._parity import run_cli
from tests._scenarios import Scenario

PROFILE = settings(max_examples=300, deadline=None)


# ---------------------------------------------------------------------------
# OBL-HASHING-079 - slice fingerprints
# ---------------------------------------------------------------------------

#: Characters that could forge JSON structure inside a component name if the
#: fingerprint were computed over an unescaped concatenation: the quote that
#: would close a key, the colon and comma that separate members, the braces and
#: brackets that open containers, and the backslash that escapes them. Two
#: ordinary letters and two non-ASCII characters keep collisions common enough
#: that the injectivity check has something to compare.
HOSTILE_NAME_ALPHABET = '"\\:{},[]/ .-_abZ0\n\t\u00e9\u4e2d'

HOSTILE_NAMES = st.text(alphabet=HOSTILE_NAME_ALPHABET, min_size=1, max_size=6)

#: A member digest is either a real hex digest or the null a strict=False
#: recomputation records for an unavailable facet. Three literals are enough:
#: the property is about the map, not about the digests in it.
MEMBER_DIGESTS = st.one_of(st.none(), st.sampled_from(["0" * 64, "1" * 64, "2" * 64]))

DIGEST_MAPS = st.dictionaries(HOSTILE_NAMES, MEMBER_DIGESTS, min_size=1, max_size=4)


def _components_map(digests: Dict[str, Optional[str]]) -> Dict[str, dict]:
    """One lockfile components map carrying exactly these exact-facet digests."""
    return {
        name: {"fingerprints": {"exact": digest}}
        for name, digest in digests.items()
    }


def _slice_fingerprint(
    digests: Dict[str, Optional[str]],
    *,
    members: Optional[List[str]] = None,
    mode: str = "exact",
) -> str:
    definition = {
        "mode": mode,
        "components": sorted(digests) if members is None else list(members),
    }
    return _recompute_slice_entry(
        "s", definition, _components_map(digests), strict=False
    )["fingerprint"]


def _independent_slice_digest(
    digests: Dict[str, Optional[str]], *, mode: str = "exact"
) -> str:
    """SHA-256 over the mode-bound slice identity, without boundver helpers.

    `canonical_json` and `sha256_hex` are the functions under test here, so the
    oracle spells their contract out with the standard library instead of
    calling them.
    """
    encoded = json.dumps(
        {"mode": mode, "component_digests": digests},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


#: The five perturbations the obligation names, as (label, mutate, holds).
#: `holds` is True when the slice fingerprint must survive the change.
SLICE_PERTURBATIONS = {
    "an unrelated component is added": (
        lambda base: dict(base, outsider="3" * 64),
        True,
    ),
    "an unrelated component is removed": (
        lambda base: {"member": base["member"]},
        True,
    ),
    "an unrelated component's digest changes": (
        lambda base: dict(base, bystander="9" * 64),
        True,
    ),
    "the member's own digest changes": (
        lambda base: dict(base, member="8" * 64),
        False,
    ),
}


class SliceFingerprintTests(unittest.TestCase):
    """OBL-HASHING-079: a slice answers for its members and for nothing else."""

    BASE = {"member": "1" * 64, "bystander": "2" * 64}

    def _fingerprint(self, digests: Dict[str, Optional[str]]) -> str:
        return _slice_fingerprint(digests, members=["member"])

    def test_only_a_resolved_member_moves_the_slice_fingerprint(self):
        reference = self._fingerprint(self.BASE)
        for label, (mutate, holds) in SLICE_PERTURBATIONS.items():
            with self.subTest(change=label):
                after = self._fingerprint(mutate(dict(self.BASE)))
                if holds:
                    self.assertEqual(after, reference, label)
                else:
                    self.assertNotEqual(after, reference, label)

    def test_a_membership_change_moves_the_slice_fingerprint(self):
        one = _slice_fingerprint(self.BASE, members=["member"])
        both = _slice_fingerprint(self.BASE, members=["member", "bystander"])
        self.assertNotEqual(one, both)

    def test_an_unrelated_component_never_moves_a_generated_slice(self):
        """The same four perturbations, through generate rather than the helper."""
        with Scenario() as scene:
            scene.component("a", path="a", provider="path-hash", boundary=["*.json"])
            scene.component("b", path="b", provider="path-hash", boundary=["*.json"])
            scene.slice("s", mode="exact", components=["a"])
            scene.file("a/api.json", '{"a": 1}\n')
            scene.file("b/api.json", '{"b": 1}\n')
            scene.commit()
            reference = scene.slice_digest("s")

            scene.file("b/api.json", '{"b": 2}\n')
            scene.commit("edit the bystander")
            self.assertEqual(scene.slice_digest("s"), reference, "bystander edit")

            scene.component("c", path="c", provider="path-hash", boundary=["*.json"])
            scene.file("c/api.json", '{"c": 1}\n')
            scene.commit("add a third component")
            self.assertEqual(scene.slice_digest("s"), reference, "unrelated addition")

            del scene.config["components"]["b"]
            scene.commit("drop the bystander")
            self.assertEqual(scene.slice_digest("s"), reference, "unrelated removal")

            # Premise: the same observation does move when the member moves, so
            # the three assertions above are not reading a constant.
            scene.file("a/api.json", '{"a": 2}\n')
            scene.commit("edit the member")
            self.assertNotEqual(scene.slice_digest("s"), reference, "member edit")

    @PROFILE
    @given(DIGEST_MAPS)
    def test_the_fingerprint_is_sha256_over_the_mode_bound_member_map(self, digests):
        self.assertEqual(
            _slice_fingerprint(digests), _independent_slice_digest(digests)
        )

    @PROFILE
    @given(DIGEST_MAPS, DIGEST_MAPS)
    def test_two_distinct_member_maps_never_share_a_fingerprint(self, left, right):
        self.assertEqual(
            left == right,
            _slice_fingerprint(left) == _slice_fingerprint(right),
        )

    def test_a_name_that_forges_json_structure_does_not_collide(self):
        """The hand-picked pair the register named, spelled out.

        Hypothesis finds this class of input on its own, but a named example
        keeps the claim readable: a single component called ``a": "x`` must not
        hash like the two-key map its rendering resembles.
        """
        forged = self._fingerprint_of({'a": "x': None})
        plain = self._fingerprint_of({"a": "x"})
        self.assertNotEqual(forged, plain)
        trailing = self._fingerprint_of({"a\\": None, "b": None})
        leading = self._fingerprint_of({"a": None, "\\b": None})
        self.assertNotEqual(trailing, leading)

    def test_the_same_member_map_twice_produces_the_same_fingerprint(self):
        """Premise for the two tests above: equality is reachable at all."""
        digests = {'a": "x': None, "b\\": "1" * 64}
        self.assertEqual(
            self._fingerprint_of(digests), self._fingerprint_of(dict(digests))
        )

    def _fingerprint_of(self, digests: Dict[str, Optional[str]]) -> str:
        return _slice_fingerprint(digests)

    def test_a_mode_change_alone_moves_the_slice_fingerprint(self):
        """The selected facet is part of the aggregate identity."""
        digests = {"member": None}
        self.assertNotEqual(
            _slice_fingerprint(digests, mode="behavior"),
            _slice_fingerprint(digests, mode="boundary"),
        )

    def test_each_mode_matches_an_independent_recomputation(self):
        digests = {"member": None}
        for mode in ("behavior", "boundary"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    _slice_fingerprint(digests, mode=mode),
                    _independent_slice_digest(digests, mode=mode),
                )

    def test_a_mode_change_reports_distinct_slice_fingerprints(self):
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.slice("s", mode="behavior", components=["svc"])
            scene.file("svc/main.py", "x = 1\n")
            scene.commit()
            locked = scene.generate(strict=False)
            scene.config["slices"]["s"]["mode"] = "boundary"
            switched = scene.generate(strict=False)

            self.assertNotEqual(
                locked["slices"]["s"]["fingerprint"],
                switched["slices"]["s"]["fingerprint"],
            )
            self.assertNotEqual(locked["slices"]["s"], switched["slices"]["s"])

            scene.config["slices"]["s"]["mode"] = "behavior"
            scene.commit("declare the behavior slice")
            self.assertEqual(
                run_cli(scene.root, "generate", "--allow-partial").returncode, 0
            )
            scene.config["slices"]["s"]["mode"] = "boundary"
            scene.commit("switch the slice to boundary")
            result = run_cli(scene.root, "verify")

            self.assertEqual(result.returncode, 1)
            self.assertIn("SLICE MISMATCH s.boundary:", result.stdout)
            locked_fingerprint = locked["slices"]["s"]["fingerprint"][:12]
            current_fingerprint = switched["slices"]["s"]["fingerprint"][:12]
            self.assertIn(
                f"SLICE MISMATCH s.boundary: lockfile={locked_fingerprint}... "
                f"current={current_fingerprint}...",
                result.stdout,
            )


# ---------------------------------------------------------------------------
# OBL-HASHING-080 - the behavior envelope
# ---------------------------------------------------------------------------

#: The three states in which a component has no boundary digest, and the
#: provider that reaches each one. `leaf` publishes nothing and is still "ok";
#: `implicit` with no declared paths is the one legitimate "partial"; a
#: provider whose resolve raises is the "error".
ABSENT_BOUNDARY_STATES = {
    "leaf": ("leaf", [], "ok"),
    "implicit without declared paths": ("implicit", [], "partial"),
    "a provider whose resolve raises": ("custom.explode", ["boundary/*.json"], "error"),
}

BEHAVIOR_PATHS = ["behavior/*.json"]


class _ExplodingProvider:
    """A registered provider that fails at resolve time, not at load time."""

    name = "custom.explode"
    version = "1.0"

    def resolve(self, ctx):
        raise ValueError("no boundary here")


class BehaviorEnvelopeTests(unittest.TestCase):
    """OBL-HASHING-080: behavior contains boundary, cryptographically."""

    def _entry(self, scene: Scenario, provider: str, boundary: List[str]) -> dict:
        registry = create_registry()
        registry["custom.explode"] = _ExplodingProvider()
        component = {
            "path": "svc",
            "boundary": {"provider": provider, "paths": list(boundary)},
            "behavior": {"paths": list(BEHAVIOR_PATHS)},
        }
        snapshot = _capture_git_source_snapshot(scene.root, "head")
        with _SourceAccessor(scene.root, "head", snapshot=snapshot) as accessor:
            return _compute_component_entry(
                "svc", component, scene.root, "head", {}, accessor, registry,
            )

    @staticmethod
    def _envelope(inner: str, boundary_identity: str) -> str:
        return _hash_framed_entries(
            [
                ("behavior", inner.encode("ascii")),
                ("boundary", boundary_identity.encode("ascii")),
            ],
            domain=HASH_DOMAIN_BEHAVIOR,
        )

    @contextlib.contextmanager
    def _repository(self):
        with Scenario() as scene:
            scene.component(
                "svc",
                path="svc",
                provider="path-hash",
                boundary=["boundary/*.json"],
                behavior=BEHAVIOR_PATHS,
            )
            scene.file("svc/boundary/api.json", '{"boundary": 1}\n')
            scene.file("svc/behavior/notes.json", '{"behavior": 1}\n')
            scene.commit()
            yield scene

    def test_a_boundary_edit_moves_the_behavior_fingerprint_across_disjoint_paths(self):
        with self._repository() as scene:
            before = scene.fingerprints("svc")
            # Premise for "disjoint": the digest over the behavior paths alone
            # is published as the boundary fingerprint of a component whose
            # boundary selector equals its behavior selector, and that value
            # must survive an edit to a file only the real boundary selects.
            inner_before = self._entry(scene, "path-hash", BEHAVIOR_PATHS)
            scene.file("svc/boundary/api.json", '{"boundary": 2}\n')
            scene.commit("edit only the boundary file")
            after = scene.fingerprints("svc")
            inner_after = self._entry(scene, "path-hash", BEHAVIOR_PATHS)

            self.assertEqual(
                inner_before["fingerprints"]["boundary"],
                inner_after["fingerprints"]["boundary"],
                "the behavior selector must not have seen the boundary file",
            )
            self.assertNotEqual(before["boundary"], after["boundary"])
            self.assertNotEqual(before["behavior"], after["behavior"])

    def test_a_real_boundary_digest_is_bound_into_the_behavior_fingerprint(self):
        """Premise for the fallback tests: this is what the envelope looks like."""
        with self._repository() as scene:
            entry = self._entry(scene, "path-hash", BEHAVIOR_PATHS)
            inner = entry["fingerprints"]["boundary"]
            self.assertEqual(
                entry["fingerprints"]["behavior"], self._envelope(inner, inner)
            )

    def test_each_absent_boundary_state_binds_its_own_none_literal(self):
        with self._repository() as scene:
            inner = self._entry(scene, "path-hash", BEHAVIOR_PATHS)["fingerprints"][
                "boundary"
            ]
            observed = {}
            for label, (provider, boundary, status) in ABSENT_BOUNDARY_STATES.items():
                with self.subTest(state=label):
                    entry = self._entry(scene, provider, boundary)
                    self.assertEqual(entry["boundary_status"], status)
                    self.assertIsNone(entry["fingerprints"]["boundary"])
                    self.assertEqual(
                        entry["fingerprints"]["behavior"],
                        self._envelope(inner, f"none:{status}"),
                    )
                    observed[label] = entry["fingerprints"]["behavior"]
            self.assertEqual(
                len(set(observed.values())),
                len(ABSENT_BOUNDARY_STATES),
                f"absent-boundary states collapsed together: {observed}",
            )

    def test_an_absent_boundary_never_looks_like_a_real_one(self):
        with self._repository() as scene:
            real = self._entry(scene, "path-hash", ["boundary/*.json"])
            self.assertIsNotNone(real["fingerprints"]["boundary"])
            for label, (provider, boundary, _status) in (
                ABSENT_BOUNDARY_STATES.items()
            ):
                with self.subTest(state=label):
                    entry = self._entry(scene, provider, boundary)
                    self.assertNotEqual(
                        entry["fingerprints"]["behavior"],
                        real["fingerprints"]["behavior"],
                    )


# ---------------------------------------------------------------------------
# OBL-HASHING-081 - the verification baseline ratchet
# ---------------------------------------------------------------------------

#: Every axis `baseline_context` records. Enumerating the returned mapping
#: covers an axis added later automatically; this set is what makes an axis
#: silently *dropped* fail rather than disappear from the loop.
BASELINE_CONTEXT_AXES = frozenset(
    {
        "project",
        "lock_schema",
        "lock_digest",
        "config_contract",
        "source",
        "components_filter",
        "facets",
        "transitive",
        "policy_digest",
    }
)

#: How to produce a different value of the same shape, per recorded type.
AXIS_MUTATIONS = {
    str: lambda value: value + "-changed",
    bool: lambda value: not value,
    list: lambda value: value + ["extra"],
    type(None): lambda value: [],
}

_BASELINE_CONFIG = {"project": "p", "components": {"svc": {"path": "svc"}}}
_BASELINE_LOCK = {
    "schema": LOCKFILE_SCHEMA,
    "config_contract": SEMANTIC_CONFIG_VERSION,
    "project": "p",
    "components": {},
    "slices": {},
}
_FACET_POLICY = {"default": ["exact"]}

#: Subjects are letters and digits only. `_COMPONENT_FACET_RE` splits on the
#: last `.facet:` under DOTALL, so a subject containing `.exact:` would reparse
#: into a different identity and the oracle below would be wrong for a reason
#: that has nothing to do with the partition being tested.
IDENTITY_SUBJECTS = st.text(
    alphabet="abcXY0123", min_size=1, max_size=4
)
IDENTITY_KEYS = st.tuples(IDENTITY_SUBJECTS, st.sampled_from(FACETS))
IDENTITY_SETS = st.lists(IDENTITY_KEYS, min_size=0, max_size=6, unique=True)


def _issue_for(subject: str, facet: str) -> str:
    return f"MISMATCH {subject}.{facet}: lockfile=aaaaaaaaaaaa current=bbbbbbbbbbbb"


def _identity_id(subject: str, facet: str) -> str:
    """The stored identity, recomputed from the documented wire format."""
    joined = "\0".join((BASELINE_SCHEMA, "component-facet", subject, facet))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _context(**overrides: Any) -> dict:
    context = baseline_context(
        config=_BASELINE_CONFIG,
        lockfile=_BASELINE_LOCK,
        source="head",
        components_filter=["svc"],
        facets=["exact"],
        transitive=False,
        facet_policy=_FACET_POLICY,
    )
    context.update(overrides)
    return context


class BaselineContextAxisTests(unittest.TestCase):
    """OBL-HASHING-081: a baseline is valid only for the scope it recorded."""

    ISSUES = [_issue_for("svc", "exact")]

    def setUp(self):
        self.context = _context()
        self.baseline = create_baseline(self.context, self.ISSUES)

    def test_the_recorded_context_carries_exactly_the_documented_axes(self):
        self.assertEqual(set(self.context), set(BASELINE_CONTEXT_AXES))
        for axis in BASELINE_CONTEXT_AXES:
            with self.subTest(axis=axis):
                self.assertIn(axis, self.baseline)
                self.assertEqual(self.baseline[axis], self.context[axis])

    def test_an_unchanged_context_applies_the_baseline(self):
        """Premise: the refusals below are not a function that always raises."""
        new, acknowledged, stale = apply_baseline(
            self.baseline, self.context, list(self.ISSUES)
        )
        self.assertEqual(new, [])
        self.assertEqual(acknowledged, list(self.ISSUES))
        self.assertEqual(stale, [])

    def test_every_recorded_axis_refuses_the_baseline_when_it_changes(self):
        for facets in (["exact"], None):
            base = _context(facets=facets)
            baseline = create_baseline(base, self.ISSUES)
            for axis, value in sorted(base.items()):
                with self.subTest(axis=axis, facets=facets):
                    overrides = {"facets": facets}
                    overrides[axis] = AXIS_MUTATIONS[type(value)](value)
                    mutated = _context(**overrides)
                    with self.assertRaises(BaselineError) as caught:
                        apply_baseline(baseline, mutated, list(self.ISSUES))
                    self.assertEqual(
                        str(caught.exception),
                        f"verification baseline {axis} does not match this "
                        "invocation; review the changed scope and explicitly "
                        "update the baseline",
                    )

    @PROFILE
    @given(IDENTITY_SETS, IDENTITY_SETS)
    def test_apply_baseline_partitions_identities_by_set_membership(
        self, stored, observed
    ):
        context = _context()
        stored_issues = [_issue_for(*key) for key in stored]
        observed_issues = [_issue_for(*key) for key in observed]
        baseline = create_baseline(context, stored_issues)

        new, acknowledged, stale = apply_baseline(
            baseline, context, list(observed_issues)
        )

        stored_ids = {_identity_id(*key) for key in stored}
        observed_ids = {_identity_id(*key) for key in observed}
        self.assertEqual(
            acknowledged,
            [
                issue
                for issue, key in zip(observed_issues, observed)
                if _identity_id(*key) in stored_ids
            ],
        )
        self.assertEqual(
            new,
            [
                issue
                for issue, key in zip(observed_issues, observed)
                if _identity_id(*key) not in stored_ids
            ],
        )
        self.assertEqual(stale, sorted(stored_ids - observed_ids))
        self.assertEqual(len(new) + len(acknowledged), len(observed_issues))

    def test_a_resolved_stored_identity_is_reported_as_stale(self):
        new, acknowledged, stale = apply_baseline(self.baseline, self.context, [])
        self.assertEqual(new, [])
        self.assertEqual(acknowledged, [])
        self.assertEqual(stale, [_identity_id("svc", "exact")])


class BaselineRatchetSequenceTests(unittest.TestCase):
    """OBL-HASHING-081: the ratchet as a user drives it, one command at a time."""

    BASELINE = "bv.baseline.json"

    def test_a_write_apply_widen_and_shrink_sequence_holds_the_ratchet(self):
        with Scenario() as scene:
            scene.component("a", path="a", provider="leaf")
            scene.component("b", path="b", provider="leaf")
            scene.file("a/main.py", "x = 1\n")
            scene.file("b/main.py", "y = 1\n")
            scene.commit()
            self.assertEqual(run_cli(scene.root, "generate").returncode, 0)

            scene.file("a/main.py", "x = 2\n")
            scene.commit("drift a")
            drifted = run_cli(scene.root, "verify")
            self.assertEqual(drifted.returncode, 1)
            self.assertIn("MISMATCH a.exact:", drifted.stdout)

            created = run_cli(
                scene.root, "verify", "--write-baseline", self.BASELINE
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertIn(
                f"Baseline created at {self.BASELINE} with 1 reviewed violation(s).",
                created.stdout,
            )
            # An untracked baseline is invisible at source=head, so the file has
            # to be committed before it can be applied.
            scene.commit("track the baseline")

            overwrite = run_cli(
                scene.root, "verify", "--write-baseline", self.BASELINE
            )
            self.assertEqual(overwrite.returncode, 2)
            self.assertIn(
                f"ERROR: verification baseline already exists: {self.BASELINE}; "
                "use --update-baseline after reviewing current debt",
                overwrite.stderr,
            )

            applied = run_cli(scene.root, "verify", "--baseline", self.BASELINE)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertIn(
                "Acknowledged 1 known baseline violation(s); "
                "no new violations found.",
                applied.stdout,
            )

            scene.file("b/main.py", "y = 2\n")
            scene.commit("drift b as well")
            widened = run_cli(scene.root, "verify", "--baseline", self.BASELINE)
            self.assertEqual(
                widened.returncode, 1, "a new identity must keep its exit severity"
            )
            self.assertIn("MISMATCH b.exact:", widened.stdout)
            self.assertIn("KNOWN BASELINE VIOLATIONS:", widened.stdout)

            refused = run_cli(
                scene.root, "verify", "--update-baseline", self.BASELINE
            )
            self.assertEqual(refused.returncode, 2)
            self.assertIn(
                "ERROR: --update-baseline is shrink-only and current "
                "verification contains 1 new violation identity/identities; "
                "fix the new violations instead of baselining them",
                refused.stderr,
            )

            scene.file("b/main.py", "y = 1\n")
            scene.commit("fix b")
            scene.file("a/main.py", "x = 1\n")
            scene.commit("fix a")

            stale_run = run_cli(scene.root, "verify", "--baseline", self.BASELINE)
            self.assertEqual(stale_run.returncode, 0, stale_run.stderr)
            self.assertIn(
                "Baseline has 1 stale violation id(s); "
                "review and run --update-baseline to remove them.",
                stale_run.stdout,
            )
            report = json.loads(
                run_cli(
                    scene.root,
                    "verify",
                    "--baseline",
                    self.BASELINE,
                    "--format",
                    "json",
                ).stdout
            )
            stored = json.loads(
                (scene.root / self.BASELINE).read_text(encoding="utf-8")
            )
            self.assertEqual(
                report["baseline"]["stale_ids"],
                [entry["id"] for entry in stored["violations"]],
            )

            shrunk = run_cli(scene.root, "verify", "--update-baseline", self.BASELINE)
            self.assertEqual(shrunk.returncode, 0, shrunk.stderr)
            self.assertIn(
                f"Baseline updated at {self.BASELINE} with 0 reviewed violation(s).",
                shrunk.stdout,
            )
            after = json.loads(
                (scene.root / self.BASELINE).read_text(encoding="utf-8")
            )
            self.assertEqual(after["violations"], [])


# ---------------------------------------------------------------------------
# OBL-HASHING-086 - foreign lockfiles reaching diff and migrate-lock
# ---------------------------------------------------------------------------


def _bare_lock(**overrides: Any) -> dict:
    lock = {
        "schema": LOCKFILE_SCHEMA,
        "config_contract": SEMANTIC_CONFIG_VERSION,
        "project": "p",
        "config_digest": "a" * 64,
        "components": {},
        "slices": {},
    }
    lock.update(overrides)
    return lock


def _without(field: str, **overrides: Any) -> dict:
    lock = _bare_lock(**overrides)
    lock.pop(field)
    return lock


#: Every foreign-schema shape the obligation names, with the exact diagnostic
#: `require_compatible_lockfile_schemas` produces for it.
SCHEMA_REFUSALS = {
    "both schemas absent": (
        _without("schema"),
        _without("schema"),
        "lockfiles use unsupported schema None (supported schemas: "
        "'boundary-lock/v3', 'boundary-lock/v4'); regenerate both lockfiles "
        "with a supported Boundver version before diffing",
    ),
    "both schemas a list": (
        _bare_lock(schema=["boundary-lock/v3"]),
        _bare_lock(schema=["boundary-lock/v3"]),
        "lockfiles use unsupported schema ['boundary-lock/v3'] (supported "
        "schemas: 'boundary-lock/v3', 'boundary-lock/v4'); regenerate both "
        "lockfiles with a supported Boundver version before diffing",
    ),
    "both schemas an int": (
        _bare_lock(schema=3),
        _bare_lock(schema=3),
        "lockfiles use unsupported schema 3 (supported schemas: "
        "'boundary-lock/v3', 'boundary-lock/v4'); regenerate both lockfiles "
        "with a supported Boundver version before diffing",
    ),
    "both v2": (
        _bare_lock(schema="boundary-lock/v2"),
        _bare_lock(schema="boundary-lock/v2"),
        "lockfiles use unsupported schema 'boundary-lock/v2' (supported "
        "schemas: 'boundary-lock/v3', 'boundary-lock/v4'); regenerate both "
        "lockfiles with a supported Boundver version before diffing",
    ),
    "one absent, one current": (
        _without("schema"),
        _bare_lock(),
        "lockfiles use incompatible schemas (old=None, new='boundary-lock/v4'); "
        "regenerate both lockfiles with the same Boundver version before diffing",
    ),
    "v1 against v4": (
        _bare_lock(schema="boundary-lock/v1"),
        _bare_lock(),
        "lockfiles use incompatible schemas (old='boundary-lock/v1', "
        "new='boundary-lock/v4'); regenerate both lockfiles with the same "
        "Boundver version before diffing",
    ),
}

#: Every lock `migrate_lockfile` must refuse, and the axis its message names.
MIGRATION_REFUSALS = {
    "no schema field": (
        _without("schema"),
        "Lockfile has no 'schema' field",
    ),
    "schema is an int": (
        _bare_lock(schema=3),
        "Unknown lockfile schema 3.",
    ),
    "schema is a list": (
        _bare_lock(schema=["boundary-lock/v3"]),
        "Unknown lockfile schema ['boundary-lock/v3'].",
    ),
    "hash contract v1": (
        _bare_lock(schema="boundary-lock/v1"),
        "boundary-lock/v1 does not bind every file's Git mode/type and "
        "semantic configuration",
    ),
    "hash contract v2": (
        _bare_lock(schema="boundary-lock/v2"),
        "boundary-lock/v2 does not bind every file's Git mode/type and "
        "semantic configuration",
    ),
    "hash contract v3": (
        _bare_lock(schema="boundary-lock/v3"),
        "boundary-lock/v3 does not bind the complete "
        "boundver-semantic-config/v3 declaration set",
    ),
    "semantic config v1": (
        _bare_lock(config_contract="boundver-semantic-config/v1"),
        "boundary-lock/v4 uses semantic configuration contract "
        "'boundver-semantic-config/v1', but this release requires "
        "'boundver-semantic-config/v3'",
    ),
    "semantic config missing": (
        _without("config_contract"),
        "boundary-lock/v4 uses semantic configuration contract 'missing'",
    ),
    "semantic config not a string": (
        _bare_lock(config_contract=7),
        "boundary-lock/v4 uses semantic configuration contract 'missing'",
    ),
}


def _corrupt(lock: dict, mutate) -> dict:
    clone = copy.deepcopy(lock)
    mutate(clone)
    return clone


#: Malformed lock members, as (label, mutate_old, mutate_new). Every pair puts
#: *different* garbage on the two sides, so anything reported as `unchanged` is
#: a false verdict rather than an accurate one.
MALFORMED_MEMBERS = {
    "components is a list": (
        lambda lock: lock.__setitem__("components", [lock["components"]]),
        lambda lock: lock.__setitem__("components", [lock["components"]]),
    ),
    "a component entry is a string": (
        lambda lock: lock["components"].__setitem__("svc", "deadbeef"),
        lambda lock: lock["components"].__setitem__("svc", "cafef00d"),
    ),
    "a component entry is a list": (
        lambda lock: lock["components"].__setitem__("svc", ["a"]),
        lambda lock: lock["components"].__setitem__("svc", ["b"]),
    ),
    "a fingerprints member is a string": (
        lambda lock: lock["components"]["svc"].__setitem__("fingerprints", "deadbeef"),
        lambda lock: lock["components"]["svc"].__setitem__("fingerprints", "cafef00d"),
    ),
    "a fingerprints member is a list": (
        lambda lock: lock["components"]["svc"].__setitem__("fingerprints", ["a"]),
        lambda lock: lock["components"]["svc"].__setitem__("fingerprints", ["b"]),
    ),
    "slices is a list": (
        lambda lock: lock.__setitem__("slices", [lock["slices"]]),
        lambda lock: lock.__setitem__("slices", [lock["slices"]]),
    ),
    "a slice entry is a string": (
        lambda lock: lock["slices"].__setitem__("s", "aaa"),
        lambda lock: lock["slices"].__setitem__("s", "bbb"),
    ),
}

class ForeignLockfileRefusalTests(unittest.TestCase):
    """OBL-HASHING-086: a lock the pull request supplied is not a verdict."""

    def test_a_current_pair_is_accepted(self):
        """Premise: the refusals below are selective, not universal."""
        require_compatible_lockfile_schemas(_bare_lock(), _bare_lock())

    def test_every_foreign_schema_shape_is_refused_with_its_own_diagnostic(self):
        for label, (old, new, message) in SCHEMA_REFUSALS.items():
            with self.subTest(shape=label):
                with self.assertRaises(LockfileError) as caught:
                    require_compatible_lockfile_schemas(old, new)
                self.assertEqual(str(caught.exception), message)

    def test_a_non_object_lockfile_is_refused_before_any_field_is_read(self):
        for old, new in (([], _bare_lock()), (_bare_lock(), "lock")):
            with self.subTest(old=type(old).__name__, new=type(new).__name__):
                with self.assertRaises(LockfileError) as caught:
                    require_compatible_lockfile_schemas(old, new)
                self.assertEqual(
                    str(caught.exception),
                    "lockfiles must each contain a JSON object",
                )

    def test_a_current_lock_migrates(self):
        """Premise for the migration refusals: the happy path still works."""
        migrated = migrate_lockfile(_bare_lock())
        self.assertEqual(migrated["schema"], LOCKFILE_SCHEMA)
        self.assertEqual(migrated["config_contract"], SEMANTIC_CONFIG_VERSION)

    def test_every_non_equivalent_lock_is_refused_by_a_named_axis(self):
        for label, (lock, fragment) in MIGRATION_REFUSALS.items():
            with self.subTest(shape=label):
                with self.assertRaises(MigrationError) as caught:
                    migrate_lockfile(lock)
                self.assertIn(fragment, str(caught.exception))

    def test_a_refused_lock_is_never_relabelled_in_place(self):
        for label, (lock, _fragment) in MIGRATION_REFUSALS.items():
            with self.subTest(shape=label):
                before = copy.deepcopy(lock)
                with self.assertRaises(MigrationError):
                    migrate_lockfile(lock)
                self.assertEqual(lock, before)
                self.assertEqual(lock.get("schema"), before.get("schema"))
                self.assertEqual(
                    lock.get("config_contract"), before.get("config_contract")
                )


class MalformedDiffMemberTests(unittest.TestCase):
    """OBL-HASHING-086: a garbage member must never read as `unchanged`."""

    def setUp(self):
        self.scene = Scenario()
        self.addCleanup(self.scene.close)
        self.scene.component(
            "svc", path="svc", provider="path-hash", boundary=["*.json"]
        )
        self.scene.slice("s", mode="exact", components=["svc"])
        self.scene.file("svc/api.json", '{"a": 1}\n')
        self.scene.commit()
        self.lock = self.scene.generate()

    def _pair(self, label: str):
        mutate_old, mutate_new = MALFORMED_MEMBERS[label]
        return _corrupt(self.lock, mutate_old), _corrupt(self.lock, mutate_new)

    def test_a_well_formed_pair_is_reported_as_changed(self):
        """Premise: `diff_lockfiles` can tell two distinct locks apart."""
        old = copy.deepcopy(self.lock)
        new = copy.deepcopy(self.lock)
        new["components"]["svc"]["fingerprints"]["exact"] = "f" * 64
        result = diff_lockfiles(old, new)
        self.assertEqual(result["components"]["unchanged"], [])
        self.assertEqual(
            [entry["name"] for entry in result["components"]["changed"]], ["svc"]
        )

    def test_diff_refuses_every_malformed_member(self):
        for label in MALFORMED_MEMBERS:
            with self.subTest(shape=label):
                old, new = self._pair(label)
                with self.assertRaises(LockfileError):
                    diff_lockfiles(old, new)

    def test_the_shipped_validator_refuses_every_malformed_member(self):
        """The compensating control, enumerated over the same table.

        `diff` validates both documents before diffing, so none of the shapes
        above is reachable from the command line. A shape added to the table
        tomorrow is checked here automatically.
        """
        _require_diffable_lockfile(self.lock)  # premise: a real lock passes
        for label in MALFORMED_MEMBERS:
            with self.subTest(shape=label):
                old, _new = self._pair(label)
                with self.assertRaises(LockfileError) as caught:
                    _require_diffable_lockfile(old)
                self.assertIn("LOCKFILE malformed:", str(caught.exception))

    def test_the_diff_command_exits_on_a_malformed_component_entry(self):
        old, new = self._pair("a component entry is a string")
        old_path = self.scene.root / "old.lock.json"
        new_path = self.scene.root / "new.lock.json"
        old_path.write_text(json.dumps(old), encoding="utf-8")
        new_path.write_text(json.dumps(new), encoding="utf-8")
        result = run_cli(self.scene.root, "diff", str(old_path), str(new_path))
        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "ERROR: Invalid lockfile: Lockfile validation failed:", result.stderr
        )
        self.assertIn("component 'svc' must be an object", result.stderr)
        self.assertEqual(result.stdout, "")


# ---------------------------------------------------------------------------
# OBL-HASHING-108 - the name that is checked and the name that is written
# ---------------------------------------------------------------------------

FORGED_ENTRIES = [("file:forged", b"attacker")]


def _provider_class(names: List[str], *, instances: Optional[list] = None):
    """A provider whose `name` answers `names[i]` on the i-th read.

    The counter is per instance because `load_custom_providers` constructs one
    instance per config entry; a class counter would leave the second entry of
    a two-entry config starting part-way through the sequence.
    """

    class _Shifting:
        version = "1.0"

        def __init__(self) -> None:
            self.reads = 0
            if instances is not None:
                instances.append(self)

        @property
        def name(self) -> str:
            self.reads += 1
            return names[min(self.reads, len(names)) - 1]

        def resolve(self, ctx):
            return ResolvedBoundary(entries=list(FORGED_ENTRIES))

    return _Shifting


@contextlib.contextmanager
def _declared(*classes):
    """Publish each class as an importable module and yield the config entries."""
    entries = []
    previous = {}
    try:
        for index, cls in enumerate(classes):
            name = f"boundver_chunk31_provider_{index}"
            previous[name] = sys.modules.get(name)
            module = types.ModuleType(name)
            module.Provider = cls
            sys.modules[name] = module
            entries.append({"module": name, "class": "Provider"})
        yield entries
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class CustomProviderRegistrationKeyTests(unittest.TestCase):
    """OBL-HASHING-108: the checked name and the written key must be one string."""

    def test_an_honest_custom_provider_is_registered_under_its_own_name(self):
        """Premise: this loader does register providers when they behave."""
        registry = create_registry()
        with _declared(_provider_class(["custom.honest"])) as entries:
            errors = load_custom_providers(entries, True, registry=registry)
        self.assertEqual(errors, [])
        self.assertIn("custom.honest", registry)

    def test_a_constant_builtin_name_is_refused_for_every_registered_key(self):
        """Premise: the prefix check does defend every name already in use.

        The surface is read from `create_registry()` rather than listed, so a
        provider or alias added later is defended here without an edit.
        """
        pristine = create_registry()
        self.assertTrue(pristine, "the registry must not be empty")
        for key in sorted(pristine):
            with self.subTest(name=key):
                target = create_registry()
                with _declared(_provider_class([key])) as entries:
                    errors = load_custom_providers(entries, True, registry=target)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(
                    "custom provider names must start with 'custom.'", errors[0]
                )
                self.assertIs(type(target[key]), type(pristine[key]))

    def test_a_later_builtin_name_cannot_replace_the_builtin(self):
        """The first validated name is the only registration-key read."""
        registry = create_registry()
        builtin = registry["path-hash"]
        with _declared(
            _provider_class(["custom.honest"] * 3 + ["path-hash"])
        ) as entries:
            errors = load_custom_providers(entries, True, registry=registry)
        self.assertEqual(errors, [])
        self.assertIs(registry["path-hash"], builtin)
        self.assertIn("custom.honest", registry)

    def test_the_first_read_is_the_key_the_registry_is_written_with(self):
        registry = create_registry()
        builtin = registry["path-hash"]
        instances: list = []
        with _declared(
            _provider_class(["custom.honest"] * 3 + ["path-hash"], instances=instances)
        ) as entries:
            errors = load_custom_providers(entries, True, registry=registry)

        self.assertEqual(errors, [])
        self.assertEqual(len(instances), 1)
        self.assertEqual(
            instances[0].reads, 1, "the name is captured once on this path"
        )
        self.assertIs(registry["path-hash"], builtin)
        self.assertIs(registry["custom.honest"], instances[0])

    def test_an_alias_key_cannot_be_captured_by_a_later_read(self):
        registry = create_registry()
        builtin = registry["openapi-raw"]
        with _declared(
            _provider_class(["custom.honest"] * 3 + ["openapi-raw"])
        ) as entries:
            errors = load_custom_providers(entries, True, registry=registry)
        self.assertEqual(errors, [])
        self.assertIs(registry["openapi-raw"], builtin)
        self.assertIs(registry["openapi"], builtin)
        self.assertIn("custom.honest", registry)

    def test_loaded_names_records_a_key_the_registry_never_gained(self):
        """`loaded_names` is a local, so the dedup check is what exposes it.

        The first entry is registered under the captured `custom.honest` key.
        A second provider with that name is then refused as a real duplicate.
        """
        registry = create_registry()
        with _declared(
            _provider_class(["custom.honest"] * 3 + ["path-hash"]),
            _provider_class(["custom.honest"]),
        ) as entries:
            errors = load_custom_providers(entries, True, registry=registry)

        self.assertEqual(
            errors,
            ["Duplicate custom provider name 'custom.honest' in providers config"],
        )
        self.assertIn("custom.honest", registry)

    def test_a_later_builtin_name_cannot_supply_a_components_boundary_digest(self):
        with Scenario() as scene:
            scene.component(
                "svc", path="svc", provider="path-hash", boundary=["*.json"]
            )
            scene.file("svc/api.json", '{"a": 1}\n')
            scene.commit()
            honest = scene.generate()["components"]["svc"]
            self.assertEqual(honest["boundary_provider"], "path-hash")

            with _declared(
                _provider_class(["custom.honest"] * 3 + ["path-hash"])
            ) as entries:
                scene.config["providers"] = entries
                protected = scene.generate(allow_custom_providers=True)["components"][
                    "svc"
                ]

            self.assertEqual(protected["boundary_status"], "ok")
            self.assertEqual(protected["boundary_provider"], "path-hash")
            self.assertEqual(
                protected["fingerprints"]["boundary"],
                honest["fingerprints"]["boundary"],
            )
            self.assertEqual(
                protected["boundary_provider_version"],
                honest["boundary_provider_version"],
            )


if __name__ == "__main__":
    unittest.main()
