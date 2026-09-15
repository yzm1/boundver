"""Six claims about the bytes boundver hashes and the numbers it exits with.

Four of these obligations are about a wire format and two are about the numbers
CI dispatches on, and both halves fail the same way: quietly. A framing bug does
not raise, it returns a digest that happens to equal another tree's digest, and
a misclassified diagnostic does not crash, it returns 1 where 2 was meant and a
pipeline accepts a build error as reviewable drift. Nothing in the suite notices
either until someone ships the wrong thing. So the tests here are written to
have an oracle that does not come from the code under test: an independent
length-prefix writer and reader for the framing layer, a multiset comparison for
distinctness, and for the exit codes an enumeration of the diagnostic strings the
source can actually produce rather than the handful somebody remembered.

The enumeration is the awkward part. `_drift_exit_code` classifies by
human-readable message *prefix*, which means the mapping is only as good as the
list of prefixes, and that list lives nowhere - it is spread across four scopes
in three modules as f-string literals. `_enumerated_diagnostic_heads` reads them
back out with `ast` at test time: it walks `verify_lockfile`, all of
`_lockfile_validation`, `_verify_lock_preflight_issues` and `_cmd_status`,
collects every string-literal-headed expression that becomes a diagnostic, and
trims each to the text before its first substitution. Today that is 43 distinct
heads. A newly worded diagnostic appears in that set automatically and fails the
check until the table below says what it should exit with, which is the point:
the obligation quantifies over "no reachable diagnostic string", and a list
typed by hand cannot answer that.

Three findings came out of it, all pinned rather than fixed. `Config malformed:
components must be a non-empty object`, `Config malformed: slices must be an
object` and `Cannot capture <source> source: ...` are safety-class diagnostics
that `_drift_exit_code` classifies as 1, because its safety tuple carries
"Config root" and "Config invalid" but neither "Config malformed" nor "Cannot
capture". The first two are shadowed for CLI users by the `validate_config` gate
that `verify` and `status` run first; the third is reachable by any caller of
`verify_lockfile` that passes a snapshot whose source disagrees with the
requested one, which is a supported public entry point. Separately, `VENDORED
DRIFT` reads `current_comp.get("warnings", [])` and no code path in `_lockfile`
ever writes a `warnings` key into a component entry, so that diagnostic cannot
currently be emitted at all; a drifted vendored copy surfaces as `CURRENT DIGEST
ERROR` and exits 2. Each of those is an `expectedFailure` stating the obligation
next to a test pinning what happens now.

The behavior envelope needed one more correction. The register's gap says the
`none:<boundary_status>` branch is reached by generating with `--allow-partial`;
it is not. `_generation_errors` rejects `boundary_status == "error"`, and
`--allow-partial` only relaxes slice requirements - the comment at
`_lockfile.py:452` says so explicitly and the observed CLI run confirms it. Only
`none:partial` survives into a generated lock. The error identity is therefore
exercised where it lives, through `_compute_component_entry`, and all three
envelopes are rebuilt from the outside with `_hash_framed_entries` so the test
asserts the identity string rather than only that three digests differ.

Covers OBL-HASHING-069, OBL-HASHING-070, OBL-HASHING-073, OBL-HASHING-076,
OBL-HASHING-077 and OBL-HASHING-078.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import struct
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from boundver import create_registry
from boundver._git import _capture_git_source_snapshot
from boundver._hashing import (
    HASH_DOMAIN_BEHAVIOR,
    HASH_DOMAIN_BOUNDARY,
    HASH_DOMAIN_CONTENT_ONLY,
    HASH_DOMAIN_EXACT,
    HASH_FRAME_VERSION,
    _hash_framed_entries,
    _ModeAwareBytes,
)
from boundver._lockfile import (
    _SourceAccessor,
    _compute_component_entry,
    generate_lockfile,
    semantic_config_digest,
    verify_lockfile,
)
from boundver.core import (
    EXIT_BEHAVIOR,
    EXIT_BOUNDARY,
    EXIT_COMPAT,
    EXIT_DRIFT,
    EXIT_OK,
    EXIT_USAGE,
    _drift_exit_code,
)

from tests._parity import run_cli
from tests._scenarios import Scenario

_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "boundver"

_SLOW = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


# ---------------------------------------------------------------------------
# OBL-HASHING-076 / OBL-HASHING-077: the v3 framing wire format
# ---------------------------------------------------------------------------

#: The framing alphabet chosen to break a naive concatenating encoder: a NUL,
#: a run that reads as a big-endian u64 length prefix, a fragment of another
#: entry's frame, and the bytes only a surrogateescaped filename can produce.
ADVERSARIAL_FRAGMENTS = (
    "",
    "file:",
    "a",
    "\x00",
    "file:svc/a",
    "\udcff",
    "\udc80\udcfe",
    "\x00\x00\x00\x00\x00\x00\x00\x05",
)

#: Every Git mode and object type the framing layer binds an entry to, plus the
#: semantic pair plain bytes receive.
MODE_TYPE_PAIRS = (
    ("100644", "blob"),
    ("100755", "blob"),
    ("120000", "blob"),
    ("160000", "commit"),
    ("semantic", "value"),
)

_labels = st.lists(
    st.sampled_from(ADVERSARIAL_FRAGMENTS), min_size=0, max_size=3
).map("".join)

_contents = st.lists(
    st.sampled_from(
        (
            b"",
            b"\x00",
            b"A\n",
            b"\x00\x00\x00\x00\x00\x00\x00\x05file:",
            b"\xff\xfe",
        )
    ),
    min_size=0,
    max_size=3,
).map(b"".join)

_quads = st.tuples(_labels, st.sampled_from(MODE_TYPE_PAIRS), _contents).map(
    lambda item: (item[0], item[1][0], item[1][1], item[2])
)

_entry_lists = st.lists(_quads, min_size=0, max_size=6)


def _encode_entry(entry: tuple) -> Tuple[bytes, bytes, bytes, bytes]:
    """Reduce either accepted entry shape to the four byte fields it frames."""
    if len(entry) == 2:
        label, content = entry
        mode = getattr(content, "git_mode", "semantic")
        object_type = getattr(content, "git_object_type", "value")
    else:
        label, mode, object_type, content = entry
    return (
        label.encode("utf-8", errors="surrogateescape"),
        mode.encode("ascii"),
        object_type.encode("ascii"),
        bytes(content),
    )


def _prepared(entries) -> List[Tuple[bytes, bytes, bytes, bytes]]:
    """The ordered byte tuples the spec says a domain's digest covers."""
    return sorted(_encode_entry(entry) for entry in entries)


def _write_preimage(prepared, domain: str) -> bytes:
    """An independent v3 writer, built from spec/HASHING.md rather than code.

    Everything is length-prefixed with a big-endian u64 and the entry count
    precedes the entries, which is the whole of the claim being tested.
    """
    out = bytearray()
    for header in (HASH_FRAME_VERSION.encode("ascii"), domain.encode("utf-8")):
        out += struct.pack(">Q", len(header)) + header
    out += struct.pack(">Q", len(prepared))
    for entry in prepared:
        for field in entry:
            out += struct.pack(">Q", len(field)) + field
    return bytes(out)


def _read_preimage(blob: bytes):
    """The matching reader. Unique decodability is exactly this succeeding."""
    position = 0

    def take() -> bytes:
        nonlocal position
        (length,) = struct.unpack_from(">Q", blob, position)
        position += 8
        value = blob[position:position + length]
        if len(value) != length:
            raise AssertionError("frame claims more bytes than the preimage holds")
        position += length
        return value

    magic = take().decode("ascii")
    domain = take().decode("utf-8")
    (count,) = struct.unpack_from(">Q", blob, position)
    position += 8
    entries = [tuple(take() for _ in range(4)) for _ in range(count)]
    if position != len(blob):
        raise AssertionError("preimage carries trailing bytes after its entries")
    return magic, domain, entries


class FramedWireFormatTests(unittest.TestCase):
    """OBL-HASHING-076: the preimage is uniquely decodable, so digests separate."""

    @_SLOW
    @given(entries=_entry_lists)
    def test_the_preimage_decodes_back_to_the_exact_entry_multiset(self, entries):
        prepared = _prepared(entries)
        blob = _write_preimage(prepared, HASH_DOMAIN_EXACT)
        magic, domain, recovered = _read_preimage(blob)
        self.assertEqual(magic, "boundver-hash/v3")
        self.assertEqual(domain, HASH_DOMAIN_EXACT)
        self.assertEqual(recovered, prepared)

    @_SLOW
    @given(entries=_entry_lists)
    def test_the_digest_is_sha256_of_that_independently_written_preimage(
        self, entries
    ):
        expected = hashlib.sha256(
            _write_preimage(_prepared(entries), HASH_DOMAIN_EXACT)
        ).hexdigest()
        self.assertEqual(
            _hash_framed_entries(entries, domain=HASH_DOMAIN_EXACT), expected
        )

    @_SLOW
    @given(left=_entry_lists, right=_entry_lists)
    def test_entry_lists_differing_as_multisets_never_share_a_digest(
        self, left, right
    ):
        assume(_prepared(left) != _prepared(right))
        self.assertNotEqual(
            _hash_framed_entries(left, domain=HASH_DOMAIN_EXACT),
            _hash_framed_entries(right, domain=HASH_DOMAIN_EXACT),
            f"collision between {left!r} and {right!r}",
        )

    def test_a_shifted_boundary_between_label_and_content_is_not_a_collision(self):
        """The premise for the generated non-collision claim above.

        The generator can only report a collision it happens to draw; this pins
        the specific shape the framing exists to defeat, so a build where the
        strategy degenerates to a single example still fails here.
        """
        shifted = [
            ("file:svc/", b"a" + b"\x00\x00\x00\x00\x00\x00\x00\x01b"),
            ("file:svc/a", b"\x00\x00\x00\x00\x00\x00\x00\x01b"),
            ("file:svc/a\x00", b"\x00\x00\x00\x00\x00\x00\x01b"),
        ]
        digests = {
            _hash_framed_entries([entry], domain=HASH_DOMAIN_EXACT)
            for entry in shifted
        }
        self.assertEqual(len(digests), 3, "a shift of the frame boundary collided")

    def test_a_zero_length_content_still_occupies_its_own_frame(self):
        """Premise: an empty entry is not the same as no entry at all."""
        with_empty = [("file:a", b""), ("file:b", b"B")]
        without = [("file:b", b"B")]
        self.assertNotEqual(
            _hash_framed_entries(with_empty, domain=HASH_DOMAIN_EXACT),
            _hash_framed_entries(without, domain=HASH_DOMAIN_EXACT),
        )

    def test_the_domain_is_bound_before_any_entry(self):
        """Premise for reading the domain out of the decoded preimage."""
        entries = [("file:a", b"same")]
        self.assertNotEqual(
            _hash_framed_entries(entries, domain=HASH_DOMAIN_EXACT),
            _hash_framed_entries(entries, domain=HASH_DOMAIN_CONTENT_ONLY),
        )
        self.assertNotEqual(
            _hash_framed_entries(entries, domain=HASH_DOMAIN_BOUNDARY),
            _hash_framed_entries(entries, domain=HASH_DOMAIN_BEHAVIOR),
        )


class FramedOrderingAndDuplicationTests(unittest.TestCase):
    """OBL-HASHING-077: order must not leak, duplicates must not coalesce."""

    @_SLOW
    @given(entries=_entry_lists, data=st.data())
    def test_any_permutation_of_the_input_produces_one_digest(self, entries, data):
        permuted = data.draw(st.permutations(entries))
        self.assertEqual(
            _hash_framed_entries(entries, domain=HASH_DOMAIN_EXACT),
            _hash_framed_entries(permuted, domain=HASH_DOMAIN_EXACT),
        )

    @_SLOW
    @given(entries=_entry_lists)
    def test_mode_aware_two_tuples_and_explicit_four_tuples_agree(self, entries):
        pairs = [
            (label, _ModeAwareBytes(content, mode, object_type))
            for label, mode, object_type, content in entries
        ]
        self.assertEqual(
            _hash_framed_entries(pairs, domain=HASH_DOMAIN_EXACT),
            _hash_framed_entries(entries, domain=HASH_DOMAIN_EXACT),
        )

    def test_the_two_tuple_form_really_reads_metadata_off_the_bytes(self):
        """Premise for the agreement above.

        If `_ModeAwareBytes` metadata were ignored, every 2-tuple would frame
        the `semantic`/`value` discriminator and the equality test would pass by
        both sides being wrong in different ways. This shows the mode carried on
        the bytes changes the digest, so the agreement means what it says.
        """
        content = b"A\n"
        regular = [("file:a", _ModeAwareBytes(content, "100644", "blob"))]
        executable = [("file:a", _ModeAwareBytes(content, "100755", "blob"))]
        plain = [("file:a", content)]
        self.assertNotEqual(
            _hash_framed_entries(regular, domain=HASH_DOMAIN_EXACT),
            _hash_framed_entries(executable, domain=HASH_DOMAIN_EXACT),
        )
        self.assertNotEqual(
            _hash_framed_entries(regular, domain=HASH_DOMAIN_EXACT),
            _hash_framed_entries(plain, domain=HASH_DOMAIN_EXACT),
        )
        self.assertEqual(
            _hash_framed_entries(plain, domain=HASH_DOMAIN_EXACT),
            _hash_framed_entries(
                [("file:a", "semantic", "value", content)],
                domain=HASH_DOMAIN_EXACT,
            ),
        )

    @_SLOW
    @given(entries=st.lists(_quads, min_size=1, max_size=5))
    def test_duplicating_one_entry_changes_the_digest(self, entries):
        doubled = [*entries, entries[0]]
        self.assertNotEqual(
            _hash_framed_entries(entries, domain=HASH_DOMAIN_EXACT),
            _hash_framed_entries(doubled, domain=HASH_DOMAIN_EXACT),
        )

    @_SLOW
    @given(entries=st.lists(_quads, min_size=1, max_size=5))
    def test_a_duplicate_is_counted_rather_than_coalesced(self, entries):
        """The entry_count in the preimage must grow, not the ordering only.

        Asserting the digest merely changed would also pass if the duplicate
        were dropped and the count left at n; the independent writer pins that
        the framed count is n+1 and the duplicate frame is present twice.
        """
        doubled = [*entries, entries[0]]
        prepared = _prepared(doubled)
        self.assertEqual(len(prepared), len(entries) + 1)
        self.assertEqual(
            _hash_framed_entries(doubled, domain=HASH_DOMAIN_EXACT),
            hashlib.sha256(
                _write_preimage(prepared, HASH_DOMAIN_EXACT)
            ).hexdigest(),
        )


# ---------------------------------------------------------------------------
# OBL-HASHING-069: the behavior envelope
# ---------------------------------------------------------------------------

#: The three boundary shapes a component with behavior.paths can end up in, and
#: the status each produces. `error` and `partial` both leave `boundary` null,
#: which is exactly when the envelope falls back to the literal identity.
BOUNDARY_SHAPES = {
    "ok": ({"provider": "path-hash", "paths": ["openapi/*.yaml"]}, "ok"),
    "partial": ({"provider": "implicit", "paths": []}, "partial"),
    "error": ({"provider": "not-registered", "paths": ["openapi/*.yaml"]}, "error"),
}

BEHAVIOR_PATHS = {"paths": ["contract/*.md"]}


def _behavior_scenario() -> Scenario:
    scene = Scenario("envelope")
    scene.component(
        "api",
        path="services/api",
        provider="path-hash",
        boundary=["openapi/*.yaml"],
        behavior=["contract/*.md", "openapi/*.yaml"],
    )
    scene.file("services/api/openapi/v1.yaml", "openapi: 3.1.0\n")
    scene.file("services/api/contract/a.md", "hello\n")
    scene.file("services/api/src.py", "x = 1\n")
    scene.commit()
    return scene


def _entry(scene: Scenario, component: dict) -> dict:
    with _SourceAccessor(scene.root, "head") as accessor:
        return _compute_component_entry(
            "api", component, scene.root, "head", {}, accessor, create_registry()
        )


class BehaviorEnvelopeTests(unittest.TestCase):
    """OBL-HASHING-069: behavior is a cryptographic superset of boundary."""

    def setUp(self) -> None:
        self.scene = _behavior_scenario()
        self.addCleanup(self.scene.close)

    def _inner_behavior_digest(self) -> str:
        """The declared behavior digest, computed as an ordinary boundary.

        The envelope's inner value is `PathHashProvider` over `behavior.paths`,
        so declaring the same selector as a boundary reproduces it without
        reaching into the envelope code being tested.
        """
        return _entry(
            self.scene,
            {
                "path": "services/api",
                "boundary": {"provider": "path-hash", "paths": ["contract/*.md"]},
            },
        )["fingerprints"]["boundary"]

    def test_each_boundary_state_produces_the_envelope_its_identity_names(self):
        inner = self._inner_behavior_digest()
        seen: Dict[str, str] = {}
        for label, (boundary, expected_status) in BOUNDARY_SHAPES.items():
            with self.subTest(boundary=label):
                entry = _entry(
                    self.scene,
                    {
                        "path": "services/api",
                        "boundary": boundary,
                        "behavior": BEHAVIOR_PATHS,
                    },
                )
                self.assertEqual(entry["boundary_status"], expected_status)
                stored = entry["fingerprints"]["boundary"]
                identity = stored if stored is not None else f"none:{expected_status}"
                if label != "ok":
                    self.assertIsNone(stored)
                    self.assertEqual(identity, f"none:{expected_status}")
                self.assertEqual(
                    entry["fingerprints"]["behavior"],
                    _hash_framed_entries(
                        [
                            ("behavior", inner.encode("ascii")),
                            ("boundary", identity.encode("ascii")),
                        ],
                        domain=HASH_DOMAIN_BEHAVIOR,
                    ),
                )
                seen[label] = entry["fingerprints"]["behavior"]
        self.assertEqual(
            len(set(seen.values())),
            3,
            f"ok, partial and error must not share a behavior identity: {seen}",
        )

    def test_the_inner_digest_alone_is_not_the_stored_behavior_fingerprint(self):
        """Premise: the envelope is applied at all.

        Every assertion above compares against a reconstruction that includes
        the boundary identity. If `behavior` were stored unwrapped, those would
        fail, but only in a way that reads as an arithmetic mistake. This says
        plainly that the stored value is not the declared digest.
        """
        inner = self._inner_behavior_digest()
        entry = _entry(
            self.scene,
            {
                "path": "services/api",
                "boundary": BOUNDARY_SHAPES["ok"][0],
                "behavior": BEHAVIOR_PATHS,
            },
        )
        self.assertNotEqual(entry["fingerprints"]["behavior"], inner)

    def test_a_file_only_the_boundary_selects_rotates_the_behavior_digest(self):
        component = {
            "path": "services/api",
            "boundary": BOUNDARY_SHAPES["ok"][0],
            "behavior": BEHAVIOR_PATHS,
        }
        before = _entry(self.scene, component)["fingerprints"]
        self.scene.file(
            "services/api/openapi/v1.yaml", "openapi: 3.1.0\ninfo: {}\n"
        )
        self.scene.commit("edit an artifact behavior.paths does not select")
        after = _entry(self.scene, component)["fingerprints"]
        self.assertNotEqual(before["boundary"], after["boundary"])
        self.assertNotEqual(
            before["behavior"],
            after["behavior"],
            "a boundary-only change left the behavior fingerprint intact",
        )

    def test_a_file_neither_selector_names_leaves_both_digests_alone(self):
        """Premise for the rotation above: the fixture is not rotating on its own.

        `services/api/src.py` is inside the component path and therefore inside
        the exact digest, so it moves something on every commit. If it also
        moved the behavior digest, the test above would prove nothing about the
        envelope.
        """
        component = {
            "path": "services/api",
            "boundary": BOUNDARY_SHAPES["ok"][0],
            "behavior": BEHAVIOR_PATHS,
        }
        before = _entry(self.scene, component)["fingerprints"]
        self.scene.file("services/api/src.py", "x = 2\n")
        self.scene.commit("edit an unselected file")
        after = _entry(self.scene, component)["fingerprints"]
        self.assertNotEqual(before["exact"], after["exact"])
        self.assertEqual(before["boundary"], after["boundary"])
        self.assertEqual(before["behavior"], after["behavior"])

    def test_a_generated_lock_can_only_ever_carry_the_partial_identity(self):
        """The register's `--allow-partial` recipe for `none:error` is stale.

        `_generation_errors` rejects `boundary_status == "error"` before a lock
        is returned and `--allow-partial` relaxes slice requirements only, so
        the error identity is unreachable through generation. Recording it here
        keeps the finding from being re-derived, and pins that `none:partial`
        does reach a real lock.
        """
        with Scenario("partial-lock") as scene:
            scene.component(
                "api",
                path="services/api",
                provider="implicit",
                boundary=[],
                behavior=["contract/*.md"],
            )
            scene.file("services/api/contract/a.md", "hello\n")
            scene.file("services/api/src.py", "x = 1\n")
            scene.commit()
            for strict in (True, False):
                with self.subTest(strict=strict):
                    entry = generate_lockfile(
                        scene.config, scene.root, strict=strict
                    )["components"]["api"]
                    self.assertEqual(entry["boundary_status"], "partial")
                    self.assertIsNone(entry["fingerprints"]["boundary"])
                    self.assertIsNotNone(entry["fingerprints"]["behavior"])

        with Scenario("error-lock") as scene:
            scene.component(
                "api",
                path="services/api",
                provider="path-hash",
                boundary=["openapi/*.yaml"],
                behavior=["contract/*.md"],
            )
            scene.file("services/api/contract/a.md", "hello\n")
            scene.file("services/api/openapi/v1.yaml", "openapi: 3.1.0\n")
            scene.commit()
            scene.config["components"]["api"]["boundary"]["provider"] = (
                "not-registered"
            )
            scene.commit("declare a provider that is not registered")
            result = run_cli(scene.root, "generate", "--allow-partial")
            self.assertEqual(result.returncode, EXIT_USAGE, result.stdout)
            self.assertIn(
                "unsupported boundary.provider 'not-registered'", result.stderr
            )


# ---------------------------------------------------------------------------
# OBL-HASHING-070: the diagnostic-to-exit-code mapping
# ---------------------------------------------------------------------------

#: Where a string can become an item in a list `_drift_exit_code` classifies.
#: core.py:1628 classifies `_verify_lock_preflight_issues`, core.py:1881
#: classifies `verify_lockfile`, and core.py:2428 classifies the `status`
#: payload; `_lockfile_validation` supplies both lock-issue producers.
DIAGNOSTIC_SCOPES = (
    ("_lockfile.py", "verify_lockfile"),
    ("_lockfile_validation.py", None),
    ("core.py", "_verify_lock_preflight_issues"),
    ("core.py", "_cmd_status"),
)

#: Names a diagnostic string is written into before it reaches a caller.
_DIAGNOSTIC_SINKS = {
    "issues",
    "structural_issues",
    "messages",
    "message",
    "errors",
}


def _literal_head(node: ast.AST) -> Optional[str]:
    """The static text a message expression begins with, or None if dynamic."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        first = node.values[0] if node.values else None
        return first.value if isinstance(first, ast.Constant) else ""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_head(node.left)
    return None


def _string_leaves(node: ast.AST) -> List[ast.AST]:
    """Unwrap comprehensions, list literals and calls down to their strings."""
    if isinstance(node, (ast.ListComp, ast.GeneratorExp, ast.SetComp)):
        return _string_leaves(node.elt)
    if isinstance(node, ast.List):
        return [leaf for elt in node.elts for leaf in _string_leaves(elt)]
    if isinstance(node, ast.Call):
        return [leaf for arg in node.args for leaf in _string_leaves(arg)]
    return [node]


def _scope_node(path: Path, function: Optional[str]) -> ast.AST:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    if function is None:
        return tree
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            return node
    raise AssertionError(f"{path.name} no longer defines {function}")


def _enumerated_diagnostic_heads() -> Dict[str, List[str]]:
    """Every literal diagnostic prefix the classified scopes can produce.

    Returned as head -> source locations so a failure names the line that added
    an unclassified wording rather than only the string.
    """
    heads: Dict[str, List[str]] = {}
    for filename, function in DIAGNOSTIC_SCOPES:
        path = _SOURCE_ROOT / filename
        scope = _scope_node(path, function)
        candidates: List[Tuple[int, ast.AST]] = []
        for node in ast.walk(scope):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                target = node.func.value
                addressed = (
                    isinstance(target, ast.Name) and target.id in _DIAGNOSTIC_SINKS
                ) or (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "issues"
                )
                if node.func.attr in {"append", "extend"} and addressed:
                    candidates.extend((node.lineno, arg) for arg in node.args)
            elif isinstance(node, ast.Return) and isinstance(
                node.value, (ast.List, ast.Call)
            ):
                returned = node.value
                if isinstance(returned, ast.Call):
                    returned = returned.args[0] if returned.args else None
                if isinstance(returned, ast.List):
                    candidates.extend((node.lineno, elt) for elt in returned.elts)
            elif isinstance(node, ast.Assign):
                names = {t.id for t in node.targets if isinstance(t, ast.Name)}
                if names & _DIAGNOSTIC_SINKS:
                    candidates.append((node.lineno, node.value))
        for lineno, candidate in candidates:
            for leaf in _string_leaves(candidate):
                head = _literal_head(leaf)
                if head is None:
                    continue
                heads.setdefault(head.split("{")[0], []).append(
                    f"{filename}:{lineno}"
                )
    return heads


#: head -> (documented exit code, a representative message carrying that head).
#: Codes are docs/reference.md's table: 2 for the safety class, then
#: compat=5 > boundary=4 > behavior=3 > 1 for everything else.
DIAGNOSTIC_CODES: Dict[str, Tuple[int, str]] = {
    "AFFECTED CONSUMERS": (
        EXIT_DRIFT,
        "AFFECTED CONSUMERS api: team-x, web",
    ),
    "CURRENT DIGEST ERROR ": (
        EXIT_USAGE,
        "CURRENT DIGEST ERROR api: Boundary computation failed",
    ),
    "Config invalid: ": (
        EXIT_USAGE,
        "Config invalid: Slice 'public' is empty",
    ),
    "Config root must be an object": (
        EXIT_USAGE,
        "Config root must be an object",
    ),
    "Config unavailable: ": (
        EXIT_USAGE,
        "Config unavailable: Expecting value: line 1 column 1",
    ),
    "LOCKED DIGEST ERROR ": (
        EXIT_USAGE,
        "LOCKED DIGEST ERROR api: Boundary computation failed",
    ),
    "LOCKFILE component set differs from config: locked=": (
        EXIT_USAGE,
        "LOCKFILE component set differs from config: locked=['api'] "
        "configured=['web']",
    ),
    "LOCKFILE malformed: ": (
        EXIT_USAGE,
        "LOCKFILE malformed: components field names must be strings",
    ),
    "LOCKFILE malformed: $schema must be a string": (
        EXIT_USAGE,
        "LOCKFILE malformed: $schema must be a string",
    ),
    "LOCKFILE malformed: component '": (
        EXIT_USAGE,
        "LOCKFILE malformed: component 'api' must be an object",
    ),
    "LOCKFILE malformed: component names must be non-empty strings": (
        EXIT_USAGE,
        "LOCKFILE malformed: component names must be non-empty strings",
    ),
    "LOCKFILE malformed: components must be an object": (
        EXIT_USAGE,
        "LOCKFILE malformed: components must be an object",
    ),
    "LOCKFILE malformed: config_contract must be ": (
        EXIT_USAGE,
        "LOCKFILE malformed: config_contract must be a string",
    ),
    "LOCKFILE malformed: config_contract must be one of ": (
        EXIT_USAGE,
        "LOCKFILE malformed: config_contract must be one of ['x']",
    ),
    "LOCKFILE malformed: config_digest must be a lowercase SHA-256 digest": (
        EXIT_USAGE,
        "LOCKFILE malformed: config_digest must be a lowercase SHA-256 digest",
    ),
    "LOCKFILE malformed: project must be a non-empty string": (
        EXIT_USAGE,
        "LOCKFILE malformed: project must be a non-empty string",
    ),
    "LOCKFILE malformed: root must be an object": (
        EXIT_USAGE,
        "LOCKFILE malformed: root must be an object",
    ),
    "LOCKFILE malformed: slice '": (
        EXIT_USAGE,
        "LOCKFILE malformed: slice 'public' must be an object",
    ),
    "LOCKFILE malformed: slice names must be non-empty strings": (
        EXIT_USAGE,
        "LOCKFILE malformed: slice names must be non-empty strings",
    ),
    "LOCKFILE malformed: slices must be an object": (
        EXIT_USAGE,
        "LOCKFILE malformed: slices must be an object",
    ),
    "LOCKFILE malformed: unknown field in ": (
        EXIT_USAGE,
        "LOCKFILE malformed: unknown field in components: 'zzz'",
    ),
    "LOCKFILE schema missing (expected ": (
        EXIT_USAGE,
        "LOCKFILE schema missing (expected boundary-lock/v4)",
    ),
    "LOCKFILE schema unsupported: ": (
        EXIT_USAGE,
        "LOCKFILE schema unsupported: boundary-lock/v2",
    ),
    "LOCKFILE semantic configuration contract mismatch: ": (
        EXIT_USAGE,
        "LOCKFILE semantic configuration contract mismatch: 'boundary-lock/v4'",
    ),
    "LOCKFILE semantic configuration contract unsupported for this "
    "read-only comparison: ": (
        EXIT_USAGE,
        "LOCKFILE semantic configuration contract unsupported for this "
        "read-only comparison: 'x'",
    ),
    "LOCKFILE slice set differs from config: locked=": (
        EXIT_USAGE,
        "LOCKFILE slice set differs from config: locked=['a'] configured=['b']",
    ),
    "METADATA MISMATCH ": (
        EXIT_DRIFT,
        "METADATA MISMATCH api.version: lockfile='1.0.0' current='1.1.0'",
    ),
    "METADATA MISMATCH config_digest: lockfile=": (
        EXIT_DRIFT,
        "METADATA MISMATCH config_digest: lockfile='a' current='b'",
    ),
    "METADATA MISMATCH project: lockfile=": (
        EXIT_DRIFT,
        "METADATA MISMATCH project: lockfile='a' current='b'",
    ),
    "MISMATCH ": (
        EXIT_DRIFT,
        "MISMATCH api.exact: lockfile=aaaaaaaaaaaa... current=bbbbbbbbbbbb...",
    ),
    "NEW component not in lockfile: ": (
        EXIT_DRIFT,
        "NEW component not in lockfile: api",
    ),
    "NEW slice not in lockfile: ": (
        EXIT_DRIFT,
        "NEW slice not in lockfile: public",
    ),
    "REMOVED component still in lockfile: ": (
        EXIT_DRIFT,
        "REMOVED component still in lockfile: api",
    ),
    "REMOVED slice still in lockfile: ": (
        EXIT_DRIFT,
        "REMOVED slice still in lockfile: public",
    ),
    "SLICE MISMATCH ": (
        EXIT_DRIFT,
        "SLICE MISMATCH public.exact: lockfile=aaaaaaaaaaaa... "
        "current=bbbbbbbbbbbb...",
    ),
    "UNAVAILABLE FACET ": (
        EXIT_USAGE,
        "UNAVAILABLE FACET api.boundary: selected gate requires both locked "
        "and current digests",
    ),
    "Unknown verification component(s): ": (
        EXIT_USAGE,
        "Unknown verification component(s): zzz",
    ),
    "Unknown verification facet(s): ": (
        EXIT_USAGE,
        "Unknown verification facet(s): zzz",
    ),
    "VENDORED DRIFT ": (
        EXIT_DRIFT,
        "VENDORED DRIFT api: vendor/api differs from source",
    ),
    "Cannot capture ": (
        EXIT_USAGE,
        "Cannot capture index source: Captured source mismatch: "
        "snapshot='head', source='index'",
    ),
    "Config malformed: components must be a non-empty object": (
        EXIT_USAGE,
        "Config malformed: components must be a non-empty object",
    ),
    "Config malformed: slices must be an object": (
        EXIT_USAGE,
        "Config malformed: slices must be an object",
    ),
    "Verification error: ": (
        EXIT_USAGE,
        "Verification error: repository state could not be read reliably",
    ),
}

#: The four facets and the exit code each carries when it is the only drift.
FACET_CODES = {
    "exact": EXIT_DRIFT,
    "behavior": EXIT_BEHAVIOR,
    "boundary": EXIT_BOUNDARY,
    "compat": EXIT_COMPAT,
}


class DiagnosticExitCodeMappingTests(unittest.TestCase):
    """OBL-HASHING-070: every emittable diagnostic has a documented code."""

    def test_the_enumerated_surface_is_the_one_the_table_classifies(self):
        found = _enumerated_diagnostic_heads()
        unclassified = {
            head: sorted(set(sites))
            for head, sites in found.items()
            if head not in DIAGNOSTIC_CODES
        }
        self.assertEqual(
            unclassified,
            {},
            "diagnostics with no documented exit code; add each to "
            "DIAGNOSTIC_CODES with the code docs/reference.md gives it",
        )
        stale = sorted(set(DIAGNOSTIC_CODES) - set(found))
        self.assertEqual(
            stale, [], "table rows for diagnostics the source no longer emits"
        )

    def test_the_enumeration_actually_reads_the_source(self):
        """Premise: an empty scan would satisfy the check above vacuously."""
        found = _enumerated_diagnostic_heads()
        self.assertGreaterEqual(len(found), 40)
        for expected in (
            "MISMATCH ",
            "SLICE MISMATCH ",
            "UNAVAILABLE FACET ",
            "VENDORED DRIFT ",
            "REMOVED component still in lockfile: ",
            "AFFECTED CONSUMERS",
            "LOCKFILE malformed: root must be an object",
        ):
            self.assertIn(expected, found)

    def test_a_new_diagnostic_wording_would_be_reported_as_unclassified(self):
        """Premise for the emptiness assertion above.

        An `assertEqual(unclassified, {})` proves nothing unless something can
        put a row in it. This runs the same comparison against a table missing
        one known row and requires the missing head to be named.
        """
        found = _enumerated_diagnostic_heads()
        reduced = {k: v for k, v in DIAGNOSTIC_CODES.items() if k != "MISMATCH "}
        unclassified = {head for head in found if head not in reduced}
        self.assertEqual(unclassified, {"MISMATCH "})

    def test_every_classified_diagnostic_exits_with_its_documented_code(self):
        for head, (expected, sample) in sorted(DIAGNOSTIC_CODES.items()):
            with self.subTest(head=head):
                self.assertTrue(
                    sample.startswith(head),
                    f"representative message does not carry head {head!r}",
                )
                self.assertEqual(_drift_exit_code([sample]), expected)

    def test_each_facet_selects_its_own_severity(self):
        for facet, expected in FACET_CODES.items():
            with self.subTest(facet=facet):
                self.assertEqual(
                    _drift_exit_code(
                        [f"MISMATCH api.{facet}: lockfile=a... current=b..."]
                    ),
                    expected,
                )
                self.assertEqual(
                    _drift_exit_code(
                        [
                            f"SLICE MISMATCH public.{facet}: "
                            "lockfile=a... current=b..."
                        ]
                    ),
                    expected,
                )

    def test_the_highest_severity_wins_and_safety_beats_all_of_them(self):
        drift = [
            "MISMATCH api.exact: lockfile=a current=b",
            "MISMATCH api.behavior: lockfile=a current=b",
            "MISMATCH api.boundary: lockfile=a current=b",
        ]
        self.assertEqual(_drift_exit_code(drift), EXIT_BOUNDARY)
        self.assertEqual(
            _drift_exit_code([*drift, "MISMATCH api.compat: lockfile=a current=b"]),
            EXIT_COMPAT,
        )
        self.assertEqual(
            _drift_exit_code([*drift, "LOCKFILE malformed: root must be an object"]),
            EXIT_USAGE,
        )

    def test_an_empty_issue_list_is_never_classified_as_success(self):
        """Observed, and worth pinning: the classifier has no zero.

        `_drift_exit_code([])` returns 1, not 0. Every call site guards on a
        non-empty list first, so this is not reachable today, but a caller that
        forgot the guard would report clean trees as drift rather than tripping
        an assertion.
        """
        self.assertEqual(_drift_exit_code([]), EXIT_DRIFT)

    def test_the_truncation_sentinel_is_classified_as_a_safety_failure(self):
        from boundver._utils import DIAGNOSTIC_TRUNCATION_SENTINEL

        self.assertTrue(
            DIAGNOSTIC_TRUNCATION_SENTINEL.startswith("DIAGNOSTICS TRUNCATED")
        )
        self.assertEqual(
            _drift_exit_code([DIAGNOSTIC_TRUNCATION_SENTINEL]), EXIT_USAGE
        )
        self.assertEqual(
            _drift_exit_code(
                [
                    "MISMATCH api.compat: lockfile=a current=b",
                    DIAGNOSTIC_TRUNCATION_SENTINEL,
                ]
            ),
            EXIT_USAGE,
        )


class DriftExitCodeSafetyTests(unittest.TestCase):
    """Safety failures outrank ordinary fingerprint drift."""

    def test_a_config_shape_failure_from_verify_is_a_safety_failure(self):
        for message in (
            "Config malformed: components must be a non-empty object",
            "Config malformed: slices must be an object",
        ):
            with self.subTest(message=message):
                self.assertEqual(_drift_exit_code([message]), EXIT_USAGE)

    def test_each_config_shape_failure_scores_as_usage(self):
        for message in (
            "Config malformed: components must be a non-empty object",
            "Config malformed: slices must be an object",
        ):
            with self.subTest(message=message):
                self.assertEqual(_drift_exit_code([message]), EXIT_USAGE)

    def test_verify_really_returns_those_two_strings(self):
        """Premise: the divergence is about messages the code can produce."""
        with Scenario("shape") as scene:
            scene.component(
                "api", path="api", provider="path-hash", boundary=["a/*.yaml"]
            )
            scene.file("api/a/x.yaml", "openapi: 3.1.0\n")
            scene.commit()
            lockfile = scene.generate()
            self.assertEqual(
                verify_lockfile(
                    {"project": "shape", "components": {}}, lockfile, scene.root
                ),
                ["Config malformed: components must be a non-empty object"],
            )
            self.assertEqual(
                verify_lockfile(
                    {
                        "project": "shape",
                        "components": scene.config["components"],
                        "slices": [],
                    },
                    lockfile,
                    scene.root,
                ),
                ["Config malformed: slices must be an object"],
            )

    def test_a_source_that_cannot_be_captured_is_a_safety_failure(self):
        self.assertEqual(
            _drift_exit_code(
                [
                    "Cannot capture index source: Captured source mismatch: "
                    "snapshot='head', source='index'"
                ]
            ),
            EXIT_USAGE,
        )

    def test_an_uncapturable_source_scores_as_usage(self):
        with Scenario("capture") as scene:
            scene.component(
                "api", path="api", provider="path-hash", boundary=["a/*.yaml"]
            )
            scene.file("api/a/x.yaml", "openapi: 3.1.0\n")
            scene.commit()
            lockfile = scene.generate()
            snapshot = _capture_git_source_snapshot(scene.root, "head")
            issues = verify_lockfile(
                scene.config,
                lockfile,
                scene.root,
                source="index",
                snapshot=snapshot,
            )
            self.assertEqual(
                issues,
                [
                    "Cannot capture index source: Captured source mismatch: "
                    "snapshot='head', source='index'"
                ],
            )
            self.assertEqual(_drift_exit_code(issues), EXIT_USAGE)

    def test_a_drifted_vendored_copy_is_reported_as_vendored_drift(self):
        scene = _vendored_scenario()
        self.addCleanup(scene.close)
        result = run_cli(scene.root, "verify", "--format", "json")
        issues = json.loads(result.stdout)["issues"]
        self.assertTrue(
            any(issue.startswith("VENDORED DRIFT ") for issue in issues), issues
        )

    def test_a_drifted_vendored_copy_exits_one_without_a_digest_error(self):
        scene = _vendored_scenario()
        self.addCleanup(scene.close)
        result = run_cli(scene.root, "verify", "--format", "json")
        self.assertEqual(result.returncode, EXIT_DRIFT, result.stderr)
        issues = json.loads(result.stdout)["issues"]
        self.assertTrue(
            issues[0].startswith(
                "VENDORED DRIFT api: Vendored copy at 'vendor/api' "
                "differs from source"
            ),
            issues,
        )
        self.assertFalse(
            any(issue.startswith("CURRENT DIGEST ERROR ") for issue in issues),
            issues,
        )

    def test_the_vendored_fixture_verifies_clean_before_the_copy_drifts(self):
        """Premise for both vendored assertions: the fixture is not born broken."""
        with Scenario("vendored-clean") as scene:
            _declare_vendored_component(scene)
            scene.commit()
            _commit_lock(scene)
            result = run_cli(scene.root, "verify", "--format", "json")
            self.assertEqual(result.returncode, EXIT_OK, result.stderr)
            self.assertEqual(json.loads(result.stdout)["issues"], [])


def _declare_vendored_component(scene: Scenario) -> None:
    scene.component(
        "api", path="services/api", provider="path-hash", boundary=["a/*.yaml"]
    )
    scene.config["components"]["api"]["vendored_copies"] = ["vendor/api"]
    scene.file("services/api/a/x.yaml", "openapi: 3.1.0\n")
    scene.file("services/api/src.py", "x = 1\n")
    scene.file("vendor/api/a/x.yaml", "openapi: 3.1.0\n")
    scene.file("vendor/api/src.py", "x = 1\n")


def _commit_lock(scene: Scenario) -> None:
    (scene.root / "boundary.lock.json").write_text(
        json.dumps(scene.generate(), indent=2) + "\n", encoding="utf-8"
    )
    scene.git("add", "--all")
    scene.git("commit", "-m", "lock")


def _vendored_scenario() -> Scenario:
    scene = Scenario("vendored")
    _declare_vendored_component(scene)
    scene.commit()
    _commit_lock(scene)
    scene.file("vendor/api/src.py", "x = 2\n")
    scene.commit("drift the vendored copy only")
    return scene


class UndominatedDriftExitCodeTests(unittest.TestCase):
    """The rows the register flagged as only ever observed under boundary drift."""

    def test_a_removed_component_alone_is_ordinary_drift(self):
        self.assertEqual(
            _drift_exit_code(["REMOVED component still in lockfile: api"]),
            EXIT_DRIFT,
        )

    def test_affected_consumers_alone_is_ordinary_drift(self):
        for message in (
            "AFFECTED CONSUMERS api: team-x, web",
            "AFFECTED CONSUMERS (TRANSITIVE) api: team-x, web",
        ):
            with self.subTest(message=message):
                self.assertEqual(_drift_exit_code([message]), EXIT_DRIFT)

    def test_affected_consumers_is_dominated_whenever_it_is_really_emitted(self):
        """Premise: the isolated classification above is a unit-level claim.

        `AFFECTED CONSUMERS` is appended only beside a gated boundary or compat
        MISMATCH, so no repository produces it alone. This runs the real
        pipeline to show both strings arrive together and the boundary code
        wins, which is why the row above had to be checked in isolation.
        """
        with Scenario("consumers") as scene:
            scene.component(
                "api",
                path="services/api",
                provider="path-hash",
                boundary=["a/*.yaml"],
                consumers=["web"],
                external_consumers=["team-x"],
            )
            scene.component(
                "web", path="services/web", provider="path-hash", boundary=["a/*.yaml"]
            )
            scene.file("services/api/a/x.yaml", "openapi: 3.1.0\n")
            scene.file("services/web/a/x.yaml", "openapi: 3.1.0\n")
            scene.commit()
            _commit_lock(scene)
            scene.file("services/api/a/x.yaml", "openapi: 3.1.0\ninfo: {}\n")
            scene.commit("boundary change")
            result = run_cli(scene.root, "verify", "--format", "json")
            issues = json.loads(result.stdout)["issues"]
            self.assertIn("AFFECTED CONSUMERS api: team-x, web", issues)
            self.assertTrue(
                any(issue.startswith("MISMATCH api.boundary:") for issue in issues),
                issues,
            )
            self.assertEqual(result.returncode, EXIT_BOUNDARY)
            self.assertEqual(_drift_exit_code(issues), EXIT_BOUNDARY)


class SafetyPrefixConsistencyTests(unittest.TestCase):
    """core.py and limit_report keep two safety tuples; they must not disagree."""

    def _safety_tuple(self, filename: str, function: str) -> Tuple[str, ...]:
        scope = _scope_node(_SOURCE_ROOT / filename, function)
        for node in ast.walk(scope):
            if not isinstance(node, ast.Assign):
                continue
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if "safety_prefixes" in names and isinstance(node.value, ast.Tuple):
                return tuple(
                    elt.value
                    for elt in node.value.elts
                    if isinstance(elt, ast.Constant)
                )
        raise AssertionError(f"{filename}:{function} no longer binds safety_prefixes")

    def test_the_reporting_filter_never_widens_the_safety_class(self):
        classifier = self._safety_tuple("core.py", "_drift_exit_code")
        reporter = self._safety_tuple("_lockfile.py", "verify_lockfile")
        self.assertTrue(
            set(reporter) <= set(classifier),
            f"limit_report treats {sorted(set(reporter) - set(classifier))} as "
            "safety while _drift_exit_code does not",
        )

    def test_fail_fast_reporting_keeps_the_exit_code_the_full_report_would_give(
        self,
    ):
        """The reason the two tuples have to agree, exercised end to end.

        `--fail-fast` reduces the report to one issue, and the exit code is
        computed from that reduced list. If the reporter dropped a safety issue
        in favour of a numerically higher facet severity, a build error would be
        handed back as boundary drift.
        """
        with Scenario("failfast") as scene:
            scene.component(
                "api",
                path="services/api",
                provider="path-hash",
                boundary=["a/*.yaml"],
                verify_facets=["exact", "boundary", "compat"],
            )
            scene.file("services/api/a/x.yaml", "openapi: 3.1.0\n")
            scene.commit()
            _commit_lock(scene)
            scene.file("services/api/a/x.yaml", "openapi: 3.1.0\ninfo: {}\n")
            scene.commit("boundary change with an unavailable compat gate")
            full = run_cli(scene.root, "verify", "--format", "json")
            fast = run_cli(scene.root, "verify", "--fail-fast", "--format", "json")
            self.assertEqual(
                json.loads(full.stdout)["ok"], False, full.stdout
            )
            self.assertEqual(
                full.returncode,
                fast.returncode,
                "fail-fast changed the exit code:\n"
                f"full={json.loads(full.stdout)['issues']}\n"
                f"fast={json.loads(fast.stdout)['issues']}",
            )
            self.assertEqual(full.returncode, EXIT_USAGE)


# ---------------------------------------------------------------------------
# OBL-HASHING-073: review endpoint rejection
# ---------------------------------------------------------------------------

#: Each way an endpoint lock can disagree with its own config or tree, and the
#: diagnostic fragment `review` must name when it refuses. Every row is applied
#: to the *base* endpoint's committed lock.
ENDPOINT_TAMPERS = {
    "schema": (
        lambda lock: lock.__setitem__("schema", "boundary-lock/v2"),
        "LOCKFILE schema unsupported: boundary-lock/v2 "
        "(expected boundary-lock/v4)",
    ),
    "config_contract": (
        lambda lock: lock.__setitem__(
            "config_contract", "boundver-semantic-config/v1"
        ),
        "LOCKFILE semantic configuration contract mismatch:",
    ),
    "project": (
        lambda lock: lock.__setitem__("project", "someone-elses-project"),
        "project differs between config and lock",
    ),
    "config_digest": (
        lambda lock: lock.__setitem__("config_digest", "0" * 64),
        "config_digest does not describe the endpoint config",
    ),
    "component_set": (
        lambda lock: lock["components"].pop("web"),
        "component set differs between config and lock",
    ),
    "consumers": (
        lambda lock: lock["components"]["api"].__setitem__("consumers", []),
        "component 'api' consumers differs between config and lock",
    ),
    "external_consumers": (
        lambda lock: lock["components"]["api"].__setitem__(
            "external_consumers", []
        ),
        "component 'api' external_consumers differs between config and lock",
    ),
    "slice_set": (
        lambda lock: lock["slices"].pop("public"),
        "slice set differs between config and lock",
    ),
    "slice_membership": (
        lambda lock: lock["slices"]["public"].__setitem__("components", ["api"]),
        "slice 'public' membership differs between config and lock",
    ),
    "slice_mode": (
        lambda lock: lock["slices"]["public"].__setitem__("mode", "exact"),
        "slice 'public' mode differs between config and lock",
    ),
    "incomplete_digests": (
        lambda lock: (
            lock["components"]["api"].__setitem__("boundary_status", "error"),
            lock["components"]["api"].__setitem__(
                "boundary_errors", ["synthetic provider failure"]
            ),
            lock["components"]["api"]["fingerprints"].__setitem__("boundary", None),
        ),
        "endpoint lock contains incomplete digests",
    ),
    "lock_lags_its_tree": (
        lambda lock: lock["components"]["api"]["fingerprints"].__setitem__(
            "exact", "1" * 64
        ),
        "unreconciled drift in 1 component",
    ),
}


def _review_repository(tamper=None) -> Tuple[Scenario, str, str]:
    """Two commits, each carrying its own config and lock, base optionally bent."""
    scene = Scenario("endpoints")
    scene.component(
        "api",
        path="services/api",
        provider="path-hash",
        boundary=["openapi/*.yaml"],
        consumers=["web"],
        external_consumers=["team-x"],
    )
    scene.component(
        "web", path="services/web", provider="path-hash", boundary=["api/*.yaml"]
    )
    scene.slice("public", mode="boundary", components=["api", "web"])
    scene.file("services/api/openapi/v1.yaml", "openapi: 3.1.0\n")
    scene.file("services/api/src.py", "x = 1\n")
    scene.file("services/web/api/w.yaml", "openapi: 3.1.0\n")
    scene.file("services/web/src.py", "y = 1\n")
    scene.commit("base tree")

    lockfile = scene.generate()
    if tamper is not None:
        tamper(lockfile)
    (scene.root / "boundary.lock.json").write_text(
        json.dumps(lockfile, indent=2) + "\n", encoding="utf-8"
    )
    scene.git("add", "--all")
    scene.git("commit", "-m", "base lock")
    base = scene.head()

    scene.file("services/api/openapi/v1.yaml", "openapi: 3.1.0\ninfo: {}\n")
    scene.commit("target tree")
    _commit_lock(scene)
    return scene, base, scene.head()


class ReviewEndpointRejectionTests(unittest.TestCase):
    """OBL-HASHING-073: an endpoint that lags its own tree is refused, not used."""

    def test_a_reconciled_pair_of_endpoints_is_accepted(self):
        """The premise every rejection below rests on.

        Without this, twelve assertions of exit 2 would be satisfied by a
        fixture that could never review cleanly for reasons of its own.
        """
        scene, base, target = _review_repository()
        self.addCleanup(scene.close)
        result = run_cli(
            scene.root, "review", "--base", base, "--target", target,
            "--format", "json",
        )
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            [row["component"] for row in payload["consumer_impact"]], ["api"]
        )

    def test_each_endpoint_inconsistency_is_refused_with_exit_two(self):
        for name, (tamper, fragment) in ENDPOINT_TAMPERS.items():
            with self.subTest(tamper=name):
                scene, base, target = _review_repository(tamper)
                try:
                    result = run_cli(
                        scene.root, "review", "--base", base, "--target", target,
                        "--format", "json",
                    )
                    self.assertEqual(
                        result.returncode,
                        EXIT_USAGE,
                        f"{name}: stdout={result.stdout} stderr={result.stderr}",
                    )
                    expected_prefix = (
                        "ERROR: review failed: Range review"
                        if name == "lock_lags_its_tree"
                        else "ERROR: review failed: base endpoint"
                    )
                    self.assertIn(expected_prefix, result.stderr)
                    self.assertIn(fragment, result.stderr)
                finally:
                    scene.close()


# ---------------------------------------------------------------------------
# OBL-HASHING-078: the semantic configuration digest
# ---------------------------------------------------------------------------

#: One config carrying every field the digest is claimed to be sensitive or
#: blind to, so a single fixture can host both transform tables.
SEMANTIC_BASE: Dict[str, Any] = {
    "$schema": "https://example.invalid/boundary.config.schema.json",
    "project": "semantic",
    "providers": [],
    "defaults": {"compat_mode": "major", "verify_facets": ["boundary", "exact"]},
    "components": {
        "api": {
            "path": "services/api",
            "ecosystem": "python",
            "note": "prose a reviewer wrote",
            "boundary": {
                "provider": "path-hash",
                "paths": ["openapi/*.yaml", "schema/*.json"],
                "note": "prose about the boundary",
                "options": {"strip_examples": True},
            },
            "behavior": {"paths": ["docs/*.md"]},
            "version_source": {"git_tag_prefix": "api-v"},
            "vendored_copies": ["vendor/api", "vendor/api-mirror"],
            "consumers": ["web", "cli"],
            "external_consumers": ["team-x", "team-y"],
            "verify_facets": ["boundary", "exact"],
        },
        "web": {"path": "services/web", "boundary": {"provider": "leaf", "paths": []}},
        "cli": {"path": "services/cli", "boundary": {"provider": "leaf", "paths": []}},
    },
    "slices": {
        "public": {"mode": "boundary", "components": ["api", "web"]},
        "downstream": {"mode": "exact", "closure_of": "api"},
    },
}

#: Edits that are presentation only. The digest must not move.
SEMANTIC_INVARIANCES = {
    "drop $schema": lambda c: c.pop("$schema"),
    "change $schema": lambda c: c.__setitem__("$schema", "urn:something-else"),
    "component ecosystem": lambda c: c["components"]["api"].__setitem__(
        "ecosystem", "node"
    ),
    "component note": lambda c: c["components"]["api"].__setitem__("note", "other"),
    "boundary note": lambda c: c["components"]["api"]["boundary"].__setitem__(
        "note", "other"
    ),
    "permute consumers": lambda c: c["components"]["api"].__setitem__(
        "consumers", ["cli", "web"]
    ),
    "permute external_consumers": lambda c: c["components"]["api"].__setitem__(
        "external_consumers", ["team-y", "team-x"]
    ),
    "permute vendored_copies": lambda c: c["components"]["api"].__setitem__(
        "vendored_copies", ["vendor/api-mirror", "vendor/api"]
    ),
    "permute defaults.verify_facets": lambda c: c["defaults"].__setitem__(
        "verify_facets", ["exact", "boundary"]
    ),
    "permute component verify_facets": lambda c: c["components"]["api"].__setitem__(
        "verify_facets", ["exact", "boundary"]
    ),
    "permute slice components": lambda c: c["slices"]["public"].__setitem__(
        "components", ["web", "api"]
    ),
    "permute boundary paths": lambda c: c["components"]["api"][
        "boundary"
    ].__setitem__("paths", ["schema/*.json", "openapi/*.yaml"]),
}

#: Edits that change what the configuration selects or gates. The digest must
#: move, or a contract-affecting change verifies clean.
SEMANTIC_SENSITIVITIES = {
    "project": lambda c: c.__setitem__("project", "renamed"),
    "providers": lambda c: c.__setitem__(
        "providers", [{"module": "m", "class": "P", "name": "custom.p"}]
    ),
    "defaults.compat_mode": lambda c: c["defaults"].__setitem__(
        "compat_mode", "semver_major_minor"
    ),
    "defaults.verify_facets membership": lambda c: c["defaults"].__setitem__(
        "verify_facets", ["boundary"]
    ),
    "component verify_facets membership": lambda c: c["components"][
        "api"
    ].__setitem__("verify_facets", ["boundary"]),
    "component path": lambda c: c["components"]["api"].__setitem__(
        "path", "services/api-v2"
    ),
    "component set": lambda c: c["components"].pop("cli"),
    "boundary provider": lambda c: c["components"]["api"]["boundary"].__setitem__(
        "provider", "openapi"
    ),
    "boundary paths": lambda c: c["components"]["api"]["boundary"].__setitem__(
        "paths", ["openapi/*.yaml"]
    ),
    "boundary options": lambda c: c["components"]["api"]["boundary"][
        "options"
    ].__setitem__("strip_examples", False),
    "behavior paths": lambda c: c["components"]["api"]["behavior"].__setitem__(
        "paths", ["docs/*.rst"]
    ),
    "version_source": lambda c: c["components"]["api"].__setitem__(
        "version_source", {"git_tag_prefix": "v"}
    ),
    "vendored_copies membership": lambda c: c["components"]["api"].__setitem__(
        "vendored_copies", ["vendor/api"]
    ),
    "consumers membership": lambda c: c["components"]["api"].__setitem__(
        "consumers", ["web"]
    ),
    "external_consumers membership": lambda c: c["components"]["api"].__setitem__(
        "external_consumers", ["team-x"]
    ),
    "slice components": lambda c: c["slices"]["public"].__setitem__(
        "components", ["api"]
    ),
    "slice mode": lambda c: c["slices"]["public"].__setitem__("mode", "exact"),
    "slice closure_of": lambda c: c["slices"]["downstream"].__setitem__(
        "closure_of", "web"
    ),
    "slice set": lambda c: c["slices"].pop("downstream"),
}


def _reordered(value: Any, order) -> Any:
    """Rebuild every JSON object with its keys in a drawn order.

    `json.loads(json.dumps(x))` preserves insertion order and therefore proves
    nothing about key order; this builds genuinely different dicts.
    """
    if isinstance(value, dict):
        items = list(value.items())
        order(items)
        return {key: _reordered(item, order) for key, item in items}
    if isinstance(value, list):
        return [_reordered(item, order) for item in value]
    return value


class SemanticConfigDigestTests(unittest.TestCase):
    """OBL-HASHING-078: presentation is free, contract is not."""

    def setUp(self) -> None:
        self.base = semantic_config_digest(SEMANTIC_BASE)

    def _mutated(self, transform) -> str:
        config = copy.deepcopy(SEMANTIC_BASE)
        transform(config)
        return semantic_config_digest(config)

    def test_presentation_edits_leave_the_digest_where_it_was(self):
        for name, transform in SEMANTIC_INVARIANCES.items():
            with self.subTest(edit=name):
                self.assertEqual(self._mutated(transform), self.base)

    def test_contract_edits_move_the_digest(self):
        moved = {}
        for name, transform in SEMANTIC_SENSITIVITIES.items():
            with self.subTest(edit=name):
                digest = self._mutated(transform)
                self.assertNotEqual(digest, self.base)
                moved[name] = digest
        self.assertEqual(
            len(set(moved.values())),
            len(moved),
            f"two different contract edits produced one digest: {moved}",
        )

    def test_the_fixture_carries_every_field_both_tables_touch(self):
        """Premise: an invariance over a field the fixture lacks is vacuous.

        `permute vendored_copies` on a config without `vendored_copies` would
        pass by mutating nothing. Each transform is applied to a deep copy and
        the copy is required to differ from the base, so a no-op edit is caught
        here rather than passing silently as an invariance.
        """
        for table, name, transform in [
            *(("invariance", n, t) for n, t in SEMANTIC_INVARIANCES.items()),
            *(("sensitivity", n, t) for n, t in SEMANTIC_SENSITIVITIES.items()),
        ]:
            with self.subTest(table=table, edit=name):
                config = copy.deepcopy(SEMANTIC_BASE)
                transform(config)
                self.assertNotEqual(
                    config, SEMANTIC_BASE, f"{name} did not change the config"
                )

    @settings(max_examples=60, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=2**32 - 1))
    def test_any_key_order_hashes_the_same(self, seed):
        import random

        shuffler = random.Random(seed)
        reordered = _reordered(SEMANTIC_BASE, shuffler.shuffle)
        self.assertEqual(semantic_config_digest(reordered), self.base)

    def test_the_reordering_helper_really_reorders(self):
        """Premise for the key-order claim: shuffling has to change something."""
        reordered = _reordered(SEMANTIC_BASE, lambda items: items.reverse())
        self.assertNotEqual(
            list(reordered.keys()), list(SEMANTIC_BASE.keys())
        )
        self.assertNotEqual(
            list(reordered["components"]["api"].keys()),
            list(SEMANTIC_BASE["components"]["api"].keys()),
        )
        self.assertEqual(
            json.dumps(reordered, sort_keys=True),
            json.dumps(SEMANTIC_BASE, sort_keys=True),
        )

    def test_the_digest_gates_verification_before_any_fingerprint(self):
        """A contract edit that selects the same bytes is still reported.

        This is the reason the invariance/sensitivity split matters: adding a
        consumer changes nothing any provider reads, so only the config digest
        can carry it into `verify`.
        """
        with Scenario("gate") as scene:
            scene.component(
                "api", path="services/api", provider="path-hash",
                boundary=["a/*.yaml"],
            )
            scene.component(
                "web", path="services/web", provider="path-hash",
                boundary=["a/*.yaml"],
            )
            scene.file("services/api/a/x.yaml", "openapi: 3.1.0\n")
            scene.file("services/web/a/x.yaml", "openapi: 3.1.0\n")
            scene.commit()
            _commit_lock(scene)
            clean = run_cli(scene.root, "verify", "--format", "json")
            self.assertEqual(clean.returncode, EXIT_OK, clean.stderr)

            scene.config["components"]["api"]["consumers"] = ["web"]
            scene.commit("declare a consumer and nothing else")
            drifted = run_cli(scene.root, "verify", "--format", "json")
            self.assertEqual(drifted.returncode, EXIT_DRIFT, drifted.stdout)
            issues = json.loads(drifted.stdout)["issues"]
            self.assertTrue(
                any(
                    issue.startswith("METADATA MISMATCH config_digest:")
                    for issue in issues
                ),
                issues,
            )


if __name__ == "__main__":
    unittest.main()
