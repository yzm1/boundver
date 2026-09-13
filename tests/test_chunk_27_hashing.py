"""Six promises about what a digest binds, and what it deliberately does not.

The behavior facet is the one place where a component's declared provider does
not decide how its files are hashed. `_lockfile._compute_component_entry` builds
a fresh `PathHashProvider` and renames it to "behavior", so a team that adopts
`openapi-canonical` to stop churning on reformats keeps churning on the behavior
digest for the same file. Every shared-file behavior test in this suite used a
raw provider, where the two facets move together and a regression that made
behavior reuse the configured provider would have gone unseen. The distinguishing
fixture is one file listed in both `boundary.paths` and `behavior.paths` under a
canonical provider; the property here reformats it, and the oracle is `json.loads`
equality on one side and a hand-written CRLF fold on the other, so neither half
of the expectation is borrowed from the module it judges. Beside it the file
rebuilds the recorded behavior digest from the raw bytes on disk, which pins not
merely that the two facets differ but that behavior hashed the file rather than
the parsed document. The envelope's second half is the safety property: behavior
is declared a cryptographic superset of boundary, so the tests hold the behavior
selection byte-identical and move the boundary underneath it, including through
all three statuses that publish no digest at all - `leaf` is `ok`, `implicit`
without paths is `partial`, a canonical provider over unparseable JSON is
`error` - and require three distinct behavior digests, which is what proves the
status string and not just the null enters the envelope.

Two of the checks had to read the product rather than a list. The five identity
fields `_read_path_content` compares across its read window are recovered from
the function's own source, so a sixth field would fail the table instead of
being silently untested; and the race itself is scripted by arming on the
bounded reader's return, so the mutated `lstat` is the post-read one by
construction rather than by counting calls. The premise for those five is a
sixth case that runs the identical harness and mutates nothing: it fires, it
returns the file's bytes, and it does not raise, which is what makes the other
five attributable to the field and not to the patching. The hash domains are
enumerated from `_hashing` by prefix for the same reason, and the CRLF reporters
are generated from `SOURCE_MODES`, so a new source mode joins the table or
breaks the coverage check rather than quietly escaping it.

Two obligations came back partly divergent. `_tree_entry_descriptors` really
does fall back to the full repository-relative label for a file outside `base`,
and the consequence is reachable: `generate_lockfile`, called as a library with
a `vendored_copies` entry that config validation would have rejected, reports an
identical vendored copy as drifted. That is pinned as an expected failure with
the current labels and the exact message beside it, and with the CLI's refusal
recorded so nobody reads the finding as a live user-facing bug. The CRLF leg is
narrower than its obligation: an unstaged rewrite is invisible to every reporter,
but once the same rewrite is committed the head and index reporters name the
path, because those two delegate to `git diff` on blob object ids while only the
working-tree comparison runs boundver's own normalization - and the exact
fingerprint does not move, so a `--changed-from` selection can name a component
whose facets are all unchanged. This host cannot create symlinks, so the symlink
arm of `_read_path_content` is untested here.

Covers OBL-HASHING-036, OBL-HASHING-039, OBL-HASHING-043, OBL-HASHING-044,
OBL-HASHING-051 and OBL-HASHING-052.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import unittest
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from unittest import mock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import boundver
from boundver import _hashing
from boundver._git import (
    _working_tree_name_status,
    changed_paths_since_ref,
    dirty_component_paths,
)
from boundver._hashing import (
    HASH_DOMAIN_BEHAVIOR,
    HASH_DOMAIN_BOUNDARY,
    _ModeAwareBytes,
    _content_only_digest,
    _hash_framed_entries,
    _read_path_content,
    _tree_entry_descriptors,
    source_tree_digest,
)
from boundver._lockfile import _SourceAccessor, _compute_component_entry
from boundver._utils import ConfigError
from boundver.providers import create_registry

from tests._parity import run_cli
from tests._scenarios import SOURCE_MODES, Scenario

PROFILE = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much],
)

#: A minimal document that every OpenAPI-canonical provider accepts, with the
#: route set left open so a property can vary the value as well as its spelling.
def _document(
    routes: Tuple[str, ...] = ("/a",),
    title: str = "t",
    version: str = "1.0.0",
) -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": title, "version": version},
        "paths": {
            route: {"get": {"responses": {"200": {"description": "ok"}}}}
            for route in routes
        },
    }


def _facets(
    scene: Scenario,
    accessor: _SourceAccessor,
    component: dict,
    registry: dict,
) -> dict:
    """One component entry read from the working tree, without a new commit."""
    return _compute_component_entry(
        "svc", component, scene.root, "working-tree", {}, accessor, registry
    )


def _folded(data: bytes) -> bytes:
    """spec/HASHING.md line 18, written out rather than imported.

    "CRLF is canonicalized to LF when content contains no NUL." Spelling the
    rule here keeps the property's oracle a reading of the document rather than
    a second call into the function whose behaviour is in question.
    """
    if b"\r\n" in data and b"\x00" not in data:
        return data.replace(b"\r\n", b"\n")
    return data


# ---------------------------------------------------------------------------
# OBL-HASHING-036: the behavior facet against the canonical boundary
# ---------------------------------------------------------------------------

#: One purely typographic rendering of a JSON document. Indentation, the two
#: separator strings, the whitespace around the value and the line ending all
#: change bytes without changing what `json.loads` returns, which is exactly the
#: "whitespace-only reformat" the obligation is about.
FORMATTING = st.fixed_dictionaries(
    {
        "indent": st.one_of(st.none(), st.integers(min_value=0, max_value=6)),
        "item_separator": st.sampled_from((",", " ,", ", ")),
        "key_separator": st.sampled_from((":", ": ", " : ")),
        "prefix": st.text(alphabet=" \t\n", max_size=4),
        "suffix": st.text(alphabet=" \t\n", max_size=4),
        "crlf": st.booleans(),
    }
)

DOCUMENTS = st.builds(
    _document,
    routes=st.lists(
        st.sampled_from(("/a", "/b", "/c", "/d")),
        min_size=1,
        max_size=3,
        unique=True,
    ).map(tuple),
    title=st.sampled_from(("t", "svc", "Service")),
    version=st.sampled_from(("1.0.0", "2.1.0")),
)


def _render(document: dict, formatting: dict) -> bytes:
    text = json.dumps(
        document,
        indent=formatting["indent"],
        separators=(formatting["item_separator"], formatting["key_separator"]),
    )
    text = formatting["prefix"] + text + formatting["suffix"]
    data = text.encode("utf-8")
    if formatting["crlf"]:
        data = data.replace(b"\n", b"\r\n")
    return data


class SharedFileBehaviorFacetTests(unittest.TestCase):
    """OBL-HASHING-036: one file, a canonical boundary and a raw behavior."""

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario()
        cls.scene.component(
            "svc",
            path="svc",
            provider="openapi-canonical",
            boundary=["api.json"],
            behavior=["api.json"],
        )
        cls.scene.file("svc/api.json", json.dumps(_document(), indent=2) + "\n")
        cls.scene.commit()
        cls.component = cls.scene.config["components"]["svc"]
        cls.registry = create_registry()
        cls.accessor = _SourceAccessor(cls.scene.root, "working-tree")
        cls.target = cls.scene.root / "svc" / "api.json"

    @classmethod
    def tearDownClass(cls):
        cls.accessor.__exit__(None, None, None)
        cls.scene.close()

    def observe(self, data: bytes) -> dict:
        """Write *data* into the tracked file and read the four facets back."""
        self.target.write_bytes(data)
        entry = _facets(self.scene, self.accessor, self.component, self.registry)
        self.assertEqual(
            entry["boundary_status"],
            "ok",
            entry.get("boundary_errors"),
        )
        return entry["fingerprints"]

    def test_a_semantic_edit_moves_the_boundary_and_the_behavior_together(self):
        """The premise: both facets react, so a later stillness means something."""
        before = self.observe(json.dumps(_document(("/a",)), indent=2).encode())
        after = self.observe(json.dumps(_document(("/a", "/b")), indent=2).encode())
        self.assertNotEqual(before["boundary"], after["boundary"])
        self.assertNotEqual(before["behavior"], after["behavior"])

    def test_a_whitespace_only_reformat_holds_the_boundary_and_moves_the_behavior(self):
        """The case the suite was missing: one file, two facets, one transform."""
        document = _document(("/a", "/b"))
        before = self.observe((json.dumps(document, indent=2) + "\n").encode())
        after = self.observe((json.dumps(document, indent=6) + "\n").encode())
        self.assertEqual(before["boundary"], after["boundary"])
        self.assertNotEqual(before["behavior"], after["behavior"])
        self.assertNotEqual(before["exact"], after["exact"])

    def test_the_recorded_behavior_digest_is_a_hash_of_the_bytes_on_disk(self):
        """Rebuild it from the file, which the canonical provider never sees.

        A regression that made the behavior facet reuse the component's
        configured provider would still produce *a* digest that moved under a
        reformat, because the exact facet moves too. What it could not produce
        is this one: the framed hash of the raw file bytes under a `file:` label
        and the file's Git mode, enveloped with the boundary identity.
        """
        for indent in (2, 6):
            with self.subTest(indent=indent):
                data = (json.dumps(_document(("/a", "/b")), indent=indent) + "\n").encode()
                fingerprints = self.observe(data)
                content = _ModeAwareBytes(
                    _folded(data), "100644", "blob", source_size=len(data)
                )
                inner = _hash_framed_entries(
                    [("file:api.json", content)], domain=HASH_DOMAIN_BOUNDARY
                )
                envelope = _hash_framed_entries(
                    [
                        ("behavior", inner.encode("ascii")),
                        ("boundary", fingerprints["boundary"].encode("ascii")),
                    ],
                    domain=HASH_DOMAIN_BEHAVIOR,
                )
                self.assertEqual(fingerprints["behavior"], envelope)

    @PROFILE
    @given(document=DOCUMENTS, first=FORMATTING, second=FORMATTING)
    def test_every_reformat_holds_the_boundary_and_tracks_the_bytes(
        self, document: dict, first: dict, second: dict
    ):
        left, right = _render(document, first), _render(document, second)
        self.assertEqual(
            json.loads(left),
            json.loads(right),
            "the strategy broke its own premise: these are not the same value",
        )
        a, b = self.observe(left), self.observe(right)
        self.assertEqual(
            a["boundary"],
            b["boundary"],
            f"canonical boundary moved under a reformat: {first} vs {second}",
        )
        if _folded(left) == _folded(right):
            self.assertEqual(
                a["behavior"],
                b["behavior"],
                f"behavior moved although the hashed bytes are equal: {first}",
            )
        else:
            self.assertNotEqual(
                a["behavior"],
                b["behavior"],
                f"behavior held although the hashed bytes differ: {first}",
            )


#: Three boundary declarations that publish no digest, one per status the
#: envelope can be handed. Each is paired with the status it produced when
#: observed, so a provider that changed which status it reports fails here
#: rather than silently collapsing two envelope identities into one.
NULL_DIGEST_BOUNDARIES: Dict[str, Tuple[dict, str]] = {
    "leaf": ({"provider": "leaf", "paths": []}, "ok"),
    "implicit-without-paths": ({"provider": "implicit", "paths": []}, "partial"),
    "canonical-over-unparseable-json": (
        {"provider": "openapi-canonical", "paths": ["broken.json"]},
        "error",
    ),
}


class BehaviorEnvelopeBindsTheBoundaryTests(unittest.TestCase):
    """OBL-HASHING-036: behavior cannot stand still while boundary moves."""

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario()
        cls.scene.component("svc", path="svc", provider="leaf")
        cls.scene.file("svc/api.json", json.dumps(_document(), indent=2) + "\n")
        cls.scene.file("svc/impl.py", "def f():\n    return 1\n")
        cls.scene.file("svc/broken.json", "{\n")
        cls.scene.commit()
        cls.registry = create_registry()
        cls.accessor = _SourceAccessor(cls.scene.root, "working-tree")
        cls.behavior_file = cls.scene.root / "svc" / "impl.py"
        cls.behavior_bytes = cls.behavior_file.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.accessor.__exit__(None, None, None)
        cls.scene.close()

    def declaration(self, boundary: dict) -> dict:
        return {
            "path": "svc",
            "boundary": boundary,
            "behavior": {"paths": ["impl.py"]},
        }

    def observe(self, boundary: dict) -> dict:
        entry = _facets(
            self.scene, self.accessor, self.declaration(boundary), self.registry
        )
        self.assertEqual(
            self.behavior_file.read_bytes(),
            self.behavior_bytes,
            "the behavior selection was disturbed; the comparison would be void",
        )
        return entry

    def canonical(self, routes: Tuple[str, ...]) -> dict:
        (self.scene.root / "svc" / "api.json").write_bytes(
            (json.dumps(_document(routes), indent=2) + "\n").encode("utf-8")
        )
        return {"provider": "openapi-canonical", "paths": ["api.json"]}

    def test_the_behavior_digest_is_stable_when_nothing_at_all_moves(self):
        """The premise: repeating the observation is not what changes it."""
        first = self.observe(self.canonical(("/a",)))
        second = self.observe(self.canonical(("/a",)))
        self.assertEqual(first["fingerprints"]["behavior"],
                         second["fingerprints"]["behavior"])
        self.assertEqual(first["fingerprints"]["boundary"],
                         second["fingerprints"]["boundary"])

    def test_a_boundary_change_rotates_a_behavior_digest_over_untouched_files(self):
        before = self.observe(self.canonical(("/a",)))
        after = self.observe(self.canonical(("/a", "/b")))
        self.assertNotEqual(
            before["fingerprints"]["boundary"], after["fingerprints"]["boundary"]
        )
        self.assertNotEqual(
            before["fingerprints"]["behavior"],
            after["fingerprints"]["behavior"],
            "behavior stood still while the boundary it covers moved",
        )

    def test_each_null_digest_boundary_status_gives_its_own_behavior_digest(self):
        """The status, not merely the absence of a digest, enters the envelope."""
        digests: Dict[str, str] = {}
        for label, (boundary, expected_status) in NULL_DIGEST_BOUNDARIES.items():
            with self.subTest(boundary=label):
                entry = self.observe(boundary)
                self.assertIsNone(entry["fingerprints"]["boundary"])
                self.assertEqual(entry["boundary_status"], expected_status)
                digests[label] = entry["fingerprints"]["behavior"]
        self.assertEqual(
            len(set(digests.values())),
            len(digests),
            f"two null-digest statuses shared a behavior digest: {digests}",
        )

    def test_a_boundary_that_publishes_a_digest_differs_from_every_null_state(self):
        published = self.observe(self.canonical(("/a",)))["fingerprints"]["behavior"]
        for label, (boundary, _status) in NULL_DIGEST_BOUNDARIES.items():
            with self.subTest(boundary=label):
                self.assertNotEqual(
                    published, self.observe(boundary)["fingerprints"]["behavior"]
                )

    @PROFILE
    @given(
        routes=st.lists(
            st.lists(
                st.sampled_from(("/a", "/b", "/c", "/d")),
                min_size=1,
                max_size=3,
                unique=True,
            ).map(tuple),
            min_size=2,
            max_size=4,
        )
    )
    def test_distinct_boundary_states_never_share_a_behavior_digest(
        self, routes: List[Tuple[str, ...]]
    ):
        """The oracle is injectivity, which needs no second implementation.

        Every observation here selects the same untouched behavior file, so the
        behavior digest is a function of the boundary state alone. Requiring
        that function to be injective on the observed boundary identities - and
        single-valued on repeats of one identity - is a statement about the
        envelope that does not depend on how the envelope is computed.
        """
        seen: Dict[Tuple[Optional[str], str], str] = {}
        for route_set in routes:
            entry = self.observe(self.canonical(route_set))
            key = (entry["fingerprints"]["boundary"], entry["boundary_status"])
            behavior = entry["fingerprints"]["behavior"]
            if key in seen:
                self.assertEqual(
                    seen[key], behavior, f"one boundary state, two digests: {key}"
                )
            else:
                self.assertNotIn(
                    behavior,
                    set(seen.values()),
                    f"{key} shares a behavior digest with another boundary state",
                )
                seen[key] = behavior


# ---------------------------------------------------------------------------
# OBL-HASHING-039: domain separation
# ---------------------------------------------------------------------------

#: Every hash domain `_hashing` declares, read from the module by prefix so a
#: domain added tomorrow is compared against the others without an edit here.
HASH_DOMAINS: Dict[str, str] = {
    name: value
    for name, value in sorted(vars(_hashing).items())
    if name.startswith("HASH_DOMAIN_")
}

#: The purposes spec/HASHING.md's domain table names, transcribed from the
#: document rather than imported, so a constant renamed on one side alone
#: fails instead of agreeing with itself.
SPEC_DOMAIN_VALUES = frozenset(
    {
        "exact-tree",
        "content-only-tree",
        "boundary",
        "behavior-envelope",
        "derivation-inputs",
        "derivation-outputs",
    }
)

#: Two entries used under every domain. Content and labels are held fixed so
#: any difference between the digests is attributable to the domain alone.
DOMAIN_PROBE_ENTRIES = (("file:a.txt", b"alpha"), ("file:b.txt", b"beta"))


class HashDomainSeparationTests(unittest.TestCase):
    """OBL-HASHING-039: each hashing purpose has a distinct domain."""

    def test_the_module_declares_exactly_the_domains_the_spec_tabulates(self):
        self.assertEqual(set(HASH_DOMAINS.values()), set(SPEC_DOMAIN_VALUES))
        self.assertEqual(
            len(HASH_DOMAINS),
            len(SPEC_DOMAIN_VALUES),
            f"a domain constant was added or duplicated: {HASH_DOMAINS}",
        )

    def test_every_declared_domain_constant_is_a_distinct_string(self):
        self.assertEqual(
            len(set(HASH_DOMAINS.values())),
            len(HASH_DOMAINS),
            f"two domain constants share a value: {HASH_DOMAINS}",
        )

    def test_one_entry_list_under_one_domain_hashes_the_same_way_twice(self):
        """The premise: the digest is a function of its inputs, not of the run.

        Without this the distinctness below could be nondeterminism rather than
        separation, and the check would prove nothing about the domain.
        """
        for name, domain in HASH_DOMAINS.items():
            with self.subTest(domain=name):
                self.assertEqual(
                    _hash_framed_entries(list(DOMAIN_PROBE_ENTRIES), domain=domain),
                    _hash_framed_entries(list(DOMAIN_PROBE_ENTRIES), domain=domain),
                )

    def test_one_entry_list_gets_a_distinct_digest_under_every_declared_domain(self):
        digests = {
            name: _hash_framed_entries(list(DOMAIN_PROBE_ENTRIES), domain=domain)
            for name, domain in HASH_DOMAINS.items()
        }
        self.assertEqual(
            len(set(digests.values())),
            len(digests),
            f"equal entries produced one digest for two purposes: {digests}",
        )

    def test_the_envelope_entry_pair_separates_from_the_raw_boundary_domain(self):
        """The confusion the separation exists to prevent, spelled out.

        `behavior` and `boundary` are the two literal labels the envelope hashes.
        A provider that published entries under those names would otherwise be
        able to produce a digest indistinguishable from an envelope.
        """
        entries = [("behavior", b"a" * 64), ("boundary", b"b" * 64)]
        self.assertNotEqual(
            _hash_framed_entries(list(entries), domain=HASH_DOMAIN_BOUNDARY),
            _hash_framed_entries(list(entries), domain=HASH_DOMAIN_BEHAVIOR),
        )


# ---------------------------------------------------------------------------
# OBL-HASHING-043: the working-tree read window
# ---------------------------------------------------------------------------

_REAL_LSTAT = Path.lstat
_REAL_BOUNDED_READ = _hashing._read_bounded_path_bytes

#: One mutation per identity field the post-read comparison reads, with the
#: value substituted into the second `lstat`. The keys are checked against the
#: function's own source below, so a sixth field fails rather than escapes.
RACE_FIELDS: Dict[str, int] = {
    "st_dev": 999999,
    "st_ino": 424242,
    "st_size": 4242,
    "st_mtime_ns": 1,
    "st_mode": 0o100755,
}


class _PostReadStat:
    """A stat_result stand-in carrying only the compared identity fields."""

    def __init__(self, real, field: Optional[str], value: Optional[int]):
        for name in RACE_FIELDS:
            setattr(self, name, getattr(real, name))
        if field is not None:
            setattr(self, field, value)


class _ScriptedRead:
    """Arm on the bounded reader's return, then script the next `lstat`.

    Counting `lstat` calls would pin the number of stats the implementation
    happens to take today. Arming on the reader instead makes "the post-read
    lstat" true by construction: the substitution happens on the first call for
    the target path after `_read_bounded_path_bytes` has already returned.
    """

    def __init__(self, target: Path, field: Optional[str] = None,
                 value: Optional[int] = None, *, vanish: bool = False):
        self.target = target
        self.field = field
        self.value = value
        self.vanish = vanish
        self.read_finished = False
        self.fired = False

    def _reader(self, *args, **kwargs):
        result = _REAL_BOUNDED_READ(*args, **kwargs)
        self.read_finished = True
        return result

    def __enter__(self) -> "_ScriptedRead":
        script = self

        # A plain function, not a bound method: only a function is a descriptor,
        # so only a function installed on Path receives the instance it is
        # called on. A bound method here silently drops `self` and every
        # unrelated `lstat` in the call raises TypeError instead.
        def lstat(path, *args, **kwargs):
            real = _REAL_LSTAT(path, *args, **kwargs)
            if script.read_finished and not script.fired and path == script.target:
                script.fired = True
                if script.vanish:
                    raise FileNotFoundError(2, "No such file or directory")
                return _PostReadStat(real, script.field, script.value)
            return real

        self._patches = [
            mock.patch.object(_hashing, "_read_bounded_path_bytes", self._reader),
            mock.patch.object(Path, "lstat", lstat),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for patch in reversed(self._patches):
            patch.stop()


class WorkingTreeReadWindowTests(unittest.TestCase):
    """OBL-HASHING-043: a file that moves under the reader gets no digest."""

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario()
        cls.scene.component("svc", path="svc", provider="leaf")
        cls.scene.file("svc/a.txt", "alpha\n")
        cls.scene.commit()
        cls.target = cls.scene.root / "svc" / "a.txt"
        cls.clean_digest = source_tree_digest(cls.scene.root, "svc", "working-tree")

        cls.empty = Scenario()
        cls.empty.component("svc", path="svc", provider="leaf")
        cls.empty.file("svc/a.txt", "")
        cls.empty.commit()
        cls.empty_digest = source_tree_digest(cls.empty.root, "svc", "working-tree")

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()
        cls.empty.close()

    def test_an_unscripted_read_returns_the_bytes_and_a_tree_digest(self):
        """The premise for every absence below: this path does produce a digest."""
        content = _read_path_content(self.scene.root, self.target, "working-tree")
        self.assertEqual(bytes(content), b"alpha\n")
        self.assertRegex(self.clean_digest, r"\A[0-9a-f]{64}\Z")

    def test_a_digest_over_empty_content_would_have_been_a_different_digest(self):
        """So "no digest" below is distinguishable from "a digest over nothing"."""
        self.assertRegex(self.empty_digest, r"\A[0-9a-f]{64}\Z")
        self.assertNotEqual(self.empty_digest, self.clean_digest)

    def test_the_scripted_lstat_fires_after_the_read_and_changes_nothing_by_itself(self):
        """The premise that makes the five cases attributable to the field.

        Same patches, same firing, no field mutated: the read succeeds and
        returns the file's bytes. Whatever the five cases below observe is
        therefore caused by the substituted value and not by the substitution.
        """
        with _ScriptedRead(self.target) as script:
            content = _read_path_content(self.scene.root, self.target, "working-tree")
        self.assertTrue(script.read_finished, "the bounded reader never ran")
        self.assertTrue(script.fired, "no lstat was scripted after the read")
        self.assertEqual(bytes(content), b"alpha\n")

    def test_the_table_lists_every_field_the_post_read_comparison_reads(self):
        """Recovered from the function, so a sixth field fails the table."""
        source = inspect.getsource(_read_path_content)
        observed = set(re.findall(r"after\.(st_\w+)", source))
        self.assertEqual(observed, set(RACE_FIELDS))

    def test_any_identity_field_changing_across_the_read_fails_closed(self):
        for field, value in RACE_FIELDS.items():
            with self.subTest(field=field):
                with _ScriptedRead(self.target, field, value) as script:
                    with self.assertRaises(ValueError) as caught:
                        _read_path_content(
                            self.scene.root, self.target, "working-tree"
                        )
                self.assertTrue(script.fired)
                self.assertEqual(
                    str(caught.exception), "File changed while hashing: svc/a.txt"
                )

    def test_no_tree_digest_is_produced_when_a_field_changes_across_the_read(self):
        for field, value in RACE_FIELDS.items():
            with self.subTest(field=field):
                with _ScriptedRead(self.target, field, value) as script:
                    with self.assertRaises(ValueError) as caught:
                        source_tree_digest(self.scene.root, "svc", "working-tree")
                self.assertTrue(script.fired)
                self.assertEqual(
                    str(caught.exception), "File changed while hashing: svc/a.txt"
                )

    def test_a_file_gone_after_a_successful_read_reports_disappearance(self):
        with _ScriptedRead(self.target, vanish=True) as script:
            with self.assertRaises(ValueError) as caught:
                _read_path_content(self.scene.root, self.target, "working-tree")
        self.assertTrue(script.fired)
        self.assertEqual(
            str(caught.exception), "File disappeared while hashing: svc/a.txt"
        )

    def test_a_file_gone_before_the_read_reports_disappearance(self):
        target = self.target

        def vanish(path, *args, **kwargs):
            if path == target:
                raise FileNotFoundError(2, "No such file or directory")
            return _REAL_LSTAT(path, *args, **kwargs)

        with mock.patch.object(Path, "lstat", vanish):
            with self.assertRaises(ValueError) as caught:
                _read_path_content(self.scene.root, self.target, "working-tree")
        self.assertEqual(
            str(caught.exception), "File disappeared while hashing: svc/a.txt"
        )


# ---------------------------------------------------------------------------
# OBL-HASHING-044: content-only labels and the vendored comparison
# ---------------------------------------------------------------------------

#: A tree small enough to read and deep enough that a label bug shows: one file
#: at the root of the component and one a directory down.
VENDORED_TREE = {"a.txt": "alpha\n", "sub/b.txt": "beta\n"}


def _vendored_scenario() -> Scenario:
    scene = Scenario()
    scene.component("svc", path="svc", provider="leaf")
    for rel, content in VENDORED_TREE.items():
        scene.file(f"svc/{rel}", content)
        scene.file(f"vendor/svc/{rel}", content)
    scene.commit()
    return scene


class ContentOnlyLabelTests(unittest.TestCase):
    """OBL-HASHING-044: the same bytes elsewhere are the same content."""

    def test_a_verbatim_copy_matches_content_only_and_differs_under_exact(self):
        with _vendored_scenario() as scene:
            for source in SOURCE_MODES:
                with self.subTest(source=source):
                    self.assertEqual(
                        _content_only_digest(scene.root, "svc", source),
                        _content_only_digest(scene.root, "vendor/svc", source),
                    )
                    self.assertNotEqual(
                        source_tree_digest(scene.root, "svc", source),
                        source_tree_digest(scene.root, "vendor/svc", source),
                    )

    def test_a_single_byte_edit_in_the_copy_breaks_the_content_only_match(self):
        """The premise: the equality above is not equality of two nulls."""
        with _vendored_scenario() as scene:
            (scene.root / "vendor" / "svc" / "sub" / "b.txt").write_bytes(b"betb\n")
            scene.commit("edit the copy")
            for source in SOURCE_MODES:
                with self.subTest(source=source):
                    self.assertNotEqual(
                        _content_only_digest(scene.root, "svc", source),
                        _content_only_digest(scene.root, "vendor/svc", source),
                    )

    def test_the_exact_and_content_only_digests_of_one_tree_differ(self):
        """Two purposes over one file set must not land on one digest."""
        with _vendored_scenario() as scene:
            for source in SOURCE_MODES:
                with self.subTest(source=source):
                    self.assertNotEqual(
                        source_tree_digest(scene.root, "svc", source),
                        _content_only_digest(scene.root, "svc", source),
                    )

    def test_a_selection_under_base_is_labelled_relative_to_the_compared_tree(self):
        """The premise for the fallback pin: the intended label is observable."""
        self.assertEqual(
            _tree_entry_descriptors(
                ["vendor/svc/a.txt", "vendor/svc/sub/b.txt"], base="vendor/svc"
            ),
            [
                (b"file:a.txt", "vendor/svc/a.txt"),
                (b"file:sub/b.txt", "vendor/svc/sub/b.txt"),
            ],
        )

    def test_a_selection_outside_base_must_not_fall_back_to_a_repository_label(self):
        """Content-only labels refuse a selection outside their declared base."""
        with self.assertRaisesRegex(ValueError, "outside base"):
            _tree_entry_descriptors(["outside/b.txt"], base="vendor/svc")

    def test_a_noncanonical_base_is_refused_instead_of_hashing_two_ways(self):
        """A leading slash cannot relabel an otherwise identical file set."""
        with _vendored_scenario() as scene:
            with self.assertRaisesRegex(ValueError, "outside base"):
                _content_only_digest(scene.root, "/vendor/svc", "head")
            self.assertEqual(
                _content_only_digest(scene.root, "svc", "head"),
                _content_only_digest(scene.root, "vendor/svc", "head"),
            )

    def test_a_library_caller_refuses_a_copy_outside_its_declared_base(self):
        with _vendored_scenario() as scene:
            scene.config["components"]["svc"]["vendored_copies"] = ["/vendor/svc"]
            with self.assertRaisesRegex(ValueError, "outside base"):
                scene.generate(source="head")

    def test_config_validation_refuses_the_shape_that_reaches_the_fallback(self):
        """What the finding above does NOT mean: the CLI is not exposed to it."""
        with _vendored_scenario() as scene:
            scene.config["components"]["svc"]["vendored_copies"] = ["/vendor/svc"]
            scene.commit("declare the vendored copy")
            result = run_cli(scene.root, "generate", "--source", "head")
        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "Component 'svc' vendored copy must be a safe repo-relative path "
            "using '/' separators: '/vendor/svc'",
            result.stderr,
        )


# ---------------------------------------------------------------------------
# OBL-HASHING-051: CRLF equivalence at the changed-path layer
# ---------------------------------------------------------------------------

#: Bytes with no NUL, so `_normalize_hash_content` folds their line endings.
TEXT_CONTENT = b"alpha\nbeta\ngamma\n"

#: The same shape with a NUL in the first line, so the fold is suppressed and
#: the identical rewrite has to be reported.
BINARY_CONTENT = b"alpha\x00beta\ngamma\n"


def _reporters() -> Dict[str, Callable[[Scenario, str], List[str]]]:
    """Every path reporter a rewrite can reach, keyed by name.

    The `changed_paths_since_ref` rows are generated from `SOURCE_MODES`, so a
    source mode added to the fixture layer joins this table rather than escaping
    it; `test_the_reporter_table_covers_every_declared_source_mode` fails if one
    ever does not.
    """
    reporters: Dict[str, Callable[[Scenario, str], List[str]]] = {
        "_working_tree_name_status": lambda scene, base: [
            path for _status, path in _working_tree_name_status(scene.root, base)
        ],
        "dirty_component_paths": lambda scene, _base: dirty_component_paths(
            scene.root, ["svc"]
        ),
    }
    for mode in SOURCE_MODES:
        reporters[f"changed_paths_since_ref({mode})"] = (
            lambda scene, base, mode=mode: changed_paths_since_ref(
                scene.root, base, mode
            )
        )
    return reporters


REPORTERS = _reporters()

#: The reporters that read the working tree. Everything else answers from
#: committed or staged state and cannot see an unstaged rewrite at all, so an
#: absence there would prove nothing.
WORKING_TREE_REPORTERS = frozenset(
    {
        "_working_tree_name_status",
        "dirty_component_paths",
        "changed_paths_since_ref(working-tree)",
    }
)

#: What each reporter says when it does see svc/f.txt. `dirty_component_paths`
#: answers in component paths, the others in file paths.
WITNESS = {
    name: (["svc"] if name == "dirty_component_paths" else ["svc/f.txt"])
    for name in REPORTERS
}


def _crlf_scenario(content: bytes) -> Scenario:
    scene = Scenario()
    scene.component("svc", path="svc", provider="leaf")
    (scene.root / "svc").mkdir(parents=True, exist_ok=True)
    (scene.root / "svc" / "f.txt").write_bytes(content)
    scene.commit()
    return scene


class CrlfEquivalenceAtTheChangedPathLayerTests(unittest.TestCase):
    """OBL-HASHING-051: a Windows checkout is not a changed repository."""

    def report(self, scene: Scenario, base: str) -> Dict[str, List[str]]:
        return {name: list(call(scene, base)) for name, call in REPORTERS.items()}

    def test_the_reporter_table_covers_every_declared_source_mode(self):
        for mode in SOURCE_MODES:
            self.assertIn(f"changed_paths_since_ref({mode})", REPORTERS)
        self.assertEqual(
            set(REPORTERS) - WORKING_TREE_REPORTERS,
            {
                f"changed_paths_since_ref({mode})"
                for mode in SOURCE_MODES
                if mode != "working-tree"
            },
        )

    def test_an_unstaged_edit_is_named_by_every_working_tree_reporter(self):
        """The premise: these reporters do speak when the bytes really change."""
        with _crlf_scenario(TEXT_CONTENT) as scene:
            base = scene.head()
            (scene.root / "svc" / "f.txt").write_bytes(TEXT_CONTENT + b"delta\n")
            observed = self.report(scene, base)
        for name in REPORTERS:
            with self.subTest(reporter=name):
                expected = WITNESS[name] if name in WORKING_TREE_REPORTERS else []
                self.assertEqual(observed[name], expected)

    def test_an_unstaged_crlf_rewrite_is_named_by_no_reporter_at_all(self):
        with _crlf_scenario(TEXT_CONTENT) as scene:
            base = scene.head()
            target = scene.root / "svc" / "f.txt"
            target.write_bytes(TEXT_CONTENT.replace(b"\n", b"\r\n"))
            self.assertNotEqual(
                target.read_bytes(),
                TEXT_CONTENT,
                "the rewrite did not change the bytes on disk",
            )
            observed = self.report(scene, base)
        for name in REPORTERS:
            with self.subTest(reporter=name):
                self.assertEqual(observed[name], [])

    def test_a_nul_bearing_file_rewritten_as_crlf_is_reported_as_changed(self):
        with _crlf_scenario(BINARY_CONTENT) as scene:
            base = scene.head()
            (scene.root / "svc" / "f.txt").write_bytes(
                BINARY_CONTENT.replace(b"\n", b"\r\n")
            )
            observed = self.report(scene, base)
        for name in REPORTERS:
            with self.subTest(reporter=name):
                expected = WITNESS[name] if name in WORKING_TREE_REPORTERS else []
                self.assertEqual(observed[name], expected)

    def test_the_exact_digest_ignores_the_crlf_rewrite_in_every_source_mode(self):
        with _crlf_scenario(TEXT_CONTENT) as scene:
            before = {
                mode: source_tree_digest(scene.root, "svc", mode)
                for mode in SOURCE_MODES
            }
            (scene.root / "svc" / "f.txt").write_bytes(
                TEXT_CONTENT.replace(b"\n", b"\r\n")
            )
            scene.git("add", "svc/f.txt")
            scene.git("commit", "-m", "crlf")
            after = {
                mode: source_tree_digest(scene.root, "svc", mode)
                for mode in SOURCE_MODES
            }
        self.assertEqual(before, after)

    def test_a_committed_crlf_rewrite_is_still_named_by_the_history_reporters(self):
        """Known behaviour, pinned: the promise is narrower than it reads.

        `changed_paths_since_ref` for head and index hands the comparison to
        `git diff`, which sees two blob object ids and no normalization, so the
        same rewrite that is invisible before `git add` is visible after it -
        while every facet digest stays where it was. Selection is conservative
        rather than wrong, but a caller cannot read a named path as a moved one.
        """
        with _crlf_scenario(TEXT_CONTENT) as scene:
            base = scene.head()
            exact_before = source_tree_digest(scene.root, "svc", "head")
            (scene.root / "svc" / "f.txt").write_bytes(
                TEXT_CONTENT.replace(b"\n", b"\r\n")
            )
            scene.git("add", "svc/f.txt")
            scene.git("commit", "-m", "crlf")
            observed = self.report(scene, base)
            exact_after = source_tree_digest(scene.root, "svc", "head")
        self.assertEqual(exact_before, exact_after)
        self.assertEqual(observed["changed_paths_since_ref(head)"], ["svc/f.txt"])
        self.assertEqual(observed["changed_paths_since_ref(index)"], ["svc/f.txt"])
        for name in sorted(WORKING_TREE_REPORTERS):
            with self.subTest(reporter=name):
                self.assertEqual(observed[name], [])


# ---------------------------------------------------------------------------
# OBL-HASHING-052: the compat facet's mode families
# ---------------------------------------------------------------------------

#: How many dotted fields of the version each configured mode keys on. The
#: oracle is plain string surgery, so it shares nothing with `parse_semver`.
MODE_KEY_FIELDS = {"major": 1, "semver_major": 1, "semver_major_minor": 2}

#: One base version and the three bumps off it. Every family-stability claim in
#: the obligation is a statement about one of these three transitions.
COMPAT_VERSIONS = ("1.2.3", "1.2.4", "1.3.0", "2.0.0")

SCHEMA_COMPAT_MODES = json.loads(
    (Path(boundver.__file__).parent / "boundary.config.schema.json").read_text(
        encoding="utf-8"
    )
)["properties"]["defaults"]["properties"]["compat_mode"]["enum"]


class CompatFacetTests(unittest.TestCase):
    """OBL-HASHING-052: the coordinated-release gate keys on the right field."""

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario()
        cls.scene.component(
            "svc",
            path="svc",
            provider="leaf",
            version_source={"file": "package.json", "field": "version"},
        )
        cls.scene.file("svc/package.json", json.dumps({"version": "1.2.3"}) + "\n")
        cls.scene.commit()
        cls.component = cls.scene.config["components"]["svc"]
        cls.registry = create_registry()
        cls.accessor = _SourceAccessor(cls.scene.root, "working-tree")

    @classmethod
    def tearDownClass(cls):
        cls.accessor.__exit__(None, None, None)
        cls.scene.close()

    def entry(self, mode: str, version: str) -> dict:
        (self.scene.root / "svc" / "package.json").write_text(
            json.dumps({"version": version}) + "\n", encoding="utf-8"
        )
        return _compute_component_entry(
            "svc",
            self.component,
            self.scene.root,
            "working-tree",
            {"compat_mode": mode},
            self.accessor,
            self.registry,
        )

    def test_the_mode_table_covers_every_mode_the_schema_declares(self):
        self.assertEqual(set(MODE_KEY_FIELDS), set(SCHEMA_COMPAT_MODES))

    def test_each_mode_keys_the_compat_digest_on_its_own_version_prefix(self):
        for mode, fields in MODE_KEY_FIELDS.items():
            for version in COMPAT_VERSIONS:
                with self.subTest(mode=mode, version=version):
                    key = ".".join(version.split(".")[:fields])
                    expected = hashlib.sha256(
                        f"svc@compat:{key}".encode("utf-8")
                    ).hexdigest()
                    self.assertEqual(
                        self.entry(mode, version)["fingerprints"]["compat"], expected
                    )

    def test_a_bump_rotates_the_compat_digest_exactly_when_its_key_changes(self):
        """Every transition at once, so no untested bump can collapse quietly."""
        for mode, fields in MODE_KEY_FIELDS.items():
            digests = {
                version: self.entry(mode, version)["fingerprints"]["compat"]
                for version in COMPAT_VERSIONS
            }
            for left in COMPAT_VERSIONS:
                for right in COMPAT_VERSIONS:
                    with self.subTest(mode=mode, bump=f"{left} -> {right}"):
                        same_family = (
                            left.split(".")[:fields] == right.split(".")[:fields]
                        )
                        if same_family:
                            self.assertEqual(digests[left], digests[right])
                        else:
                            self.assertNotEqual(digests[left], digests[right])

    def test_the_two_major_family_spellings_are_interchangeable(self):
        """`semver_major` is an alias, not a third family."""
        for version in COMPAT_VERSIONS:
            with self.subTest(version=version):
                self.assertEqual(
                    self.entry("major", version)["fingerprints"]["compat"],
                    self.entry("semver_major", version)["fingerprints"]["compat"],
                )

    def test_a_parseable_version_produces_a_digest_and_no_version_error(self):
        """The premise for the null below: this component does emit a compat."""
        entry = self.entry("major", "1.2.3")
        self.assertRegex(entry["fingerprints"]["compat"], r"\A[0-9a-f]{64}\Z")
        self.assertNotIn("version_errors", entry)

    def test_a_version_that_is_not_semver_yields_a_null_digest_and_the_error(self):
        for mode in MODE_KEY_FIELDS:
            with self.subTest(mode=mode):
                entry = self.entry(mode, "not-a-version")
                self.assertIsNone(entry["fingerprints"]["compat"])
                self.assertEqual(
                    entry["version_errors"],
                    ["Configured version is not valid SemVer: 'not-a-version'"],
                )
                self.assertEqual(
                    entry["semver"],
                    {
                        "compat_family": None,
                        "api_surface": None,
                        "exact_version": "not-a-version",
                    },
                )

    def test_generation_refuses_to_bless_a_lock_whose_version_is_not_semver(self):
        with Scenario() as scene:
            scene.component(
                "svc",
                path="svc",
                provider="leaf",
                version_source={"file": "package.json", "field": "version"},
            )
            scene.file("svc/package.json", json.dumps({"version": "not-a-version"}) + "\n")
            scene.commit()
            with self.assertRaises(ConfigError) as caught:
                scene.generate(source="head")
        self.assertEqual(
            str(caught.exception),
            "Lockfile generation failed:\n"
            "svc: Configured version is not valid SemVer: 'not-a-version'",
        )


if __name__ == "__main__":
    unittest.main()
