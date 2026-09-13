"""What a declaration selects, what a selection hashes, and where a change is reported.

Three of the six obligations here are about the same failure mode wearing
different clothes. A component publishes a contract; something about that
contract changes; and the digest does not move, so verification reports the
component unchanged. A script loses its executable bit. A renamed contract file
silently drops out of a glob. A submodule appears where a file used to be. In
each case the honest answer is either a rotated digest or a refusal, and the
dishonest answer is a digest computed over whatever survived.

The mode half of that was already tested, and skipped on Windows, which is the
maintainer's own platform: every existing mode test reaches for `os.chmod` and a
`symlink_to`, and this host records neither. The rotation does not actually need
either. `git update-index --cacheinfo` writes a canonical mode straight into the
index, and `commit_index` commits the index without the `git add --all` that
would put the filesystem's opinion back, so the same 100644 to 100755 to 120000
walk runs everywhere against one unchanging blob object id. That is what
`ModeRotationThroughTheIndexTests` does, and it measures all four digests the
obligation names - the exact tree, the content-only digest vendored copies are
compared with, and the raw boundary and behavior facets - across both captured
source modes. Those two sources are one witness reached by two routes rather
than two independent witnesses: `head` reads `ls-tree HEAD` and `index` writes
a tree first, and after `commit_index` the two trees are the same tree, so
every digest agrees across them. Both rows are worth running because the two
routes are different code, but nobody should read their agreement as
corroboration. Two things about the recorded gap turned out to be stale and are
worth saying plainly: `_working_tree_mode` and its "Unsupported working-tree
file type" message are already covered by tests/test_source_typing.py for a
directory and a FIFO, and the gitlink refusal is covered by
tests/test_blob_streaming.py. What was genuinely unpinned is the tree-record
branch of `_parse_ls_tree_record`, so that is what is added here, with `git
ls-tree` run without `-r` to show the refused shape is one real Git emits.

The selection obligations needed a fixture that keeps a matched declaration and
an unmatched one in the same list, because the branch that matters is the one
where `selected` is not empty. Every earlier fixture declared paths where
nothing matched at all, so the provider would have failed at the "produced no
digest" guard whatever `_resolve_declared_files` did with the errors. Each row
of that table carries a third column naming the declaration that would survive
it, and that declaration's digest is computed first, in its own repository, and
then asserted absent from that row's failing lock entry by value. The third
column is not decoration. A first draft computed one survivor digest for the
whole table, and because the literal row survives to a single file and the glob
row survives to two, the one digest was a string the glob row could never have
held: weakening the guard to `if errors and not selected` makes that row publish
a two-file digest, which a one-file survivor check waves straight through. Per
row, the by-value assertion fires on both. Ordering by UTF-8 bytes rather than
code points cannot be shown with real files on this host, since Windows has no
filename byte a UTF-8 decoder must surrogate-escape, so that one test drives
`PathHashProvider.resolve` through a synthetic context whose listing contains
"a" plus U+DCFF and "a" plus U+FFFF - a pair the two orderings disagree about,
which the test proves before it relies on it. Finally, the pointer-escaping
gap said swapping the two replacements in `pointer_child` would keep every
existing assertion green; that is wrong, because reversing them mangles any key
containing "/" into "~01". The real hole is the tilde escape itself, whose
absence collides the keys "~1" and "/" onto one pointer, so the file asserts
injectivity and an independent unescape rather than the ordering alone. The
first spelling of that injectivity claim asserted nothing at all. It drew two
independent `st.text()` strings, which differ, and whose pointers differ, so the
biconditional was satisfied by two Falses; deleting the tilde escape left it
green while nineteen of its neighbours went red. It now draws a key that
contains "~1" and pairs it with the key a dropped escape would fuse it to,
proves the collision against a local model of the dropped escape, and only then
asks `pointer_child` to keep them apart - so every single example kills that
mutant. A companion test walks all 259 strings of at most three characters over
the six-character alphabet where every such collision lives.

Covers OBL-GIT-SOURCE-064, OBL-GLOBS-008, OBL-GLOBS-009, OBL-GLOBS-011,
OBL-GLOBS-012 and OBL-GLOBS-025.
"""

from __future__ import annotations

import itertools
import json
import re
import unittest
from pathlib import Path
from typing import Dict, List, Optional

import jsonschema
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import boundver
from boundver._config_contract import (
    COMPONENT_IDENTIFIER_PATTERN,
    MAX_CONSUMER_IDENTIFIER_CHARS,
    component_identifier_problem,
)
from boundver._git import _collect_ls_tree_entries, _parse_ls_tree_record
from boundver._hashing import (
    HASH_DOMAIN_BOUNDARY,
    _content_only_digest,
    _hash_framed_entries,
    _ModeAwareBytes,
    source_tree_digest,
)
from boundver._lockfile import (
    _SourceAccessor,
    _compute_component_entry,
    generate_lockfile,
)
from boundver._provider_diff import (
    StructuralDiffBudget,
    _is_json_pointer,
    diff_canonical_json_entries,
)
from boundver._utils import (
    ConfigError,
    MAX_DECLARED_PATH_BYTES,
    MAX_GLOB_METACHARACTERS_PER_SEGMENT,
    MAX_GLOB_PATTERN_SEGMENT_BYTES,
    MAX_GLOB_SEGMENTS,
    _is_glob,
    _normalize_declared_path,
)
from boundver.providers import (
    ProviderContext,
    _resolve_declared_files,
    compute_boundary,
    create_registry,
)

from tests._parity import assert_variants_agree, run_cli
from tests._scenarios import Scenario

LF = chr(10)
BACKSLASH = chr(92)

#: The three canonical Git modes a blob entry can carry. A transition among
#: them is contract-relevant even when the object id does not move.
GIT_MODES = ("100644", "100755", "120000")

#: The four digests OBL-GIT-SOURCE-064 says a mode transition must rotate.
#: "content_only" is the digest vendored copies are compared with; it drops the
#: path prefix but keeps the mode, which is why it belongs in this list.
ROTATING_FACETS = ("exact", "content_only", "boundary", "behavior")

#: Every source mode that reads a captured Git tree. "working-tree" is left out
#: on purpose: an index entry written with --cacheinfo has no counterpart on
#: disk, so the two would disagree for a reason that is not the obligation's.
CAPTURED_SOURCES = ("head", "index")

PROFILE = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much],
)


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-064: the mode is part of the identity
# ---------------------------------------------------------------------------


def _mode_scenario() -> Scenario:
    """One component with a boundary file and a disjoint behavior file."""
    scene = Scenario("modes")
    scene.component(
        "svc",
        path="svc",
        provider="path-hash",
        boundary=["contract.txt"],
        behavior=["behavior.txt"],
    )
    scene.file("svc/contract.txt", "target")
    scene.file("svc/behavior.txt", "stable")
    scene.commit()
    return scene


def _facets(scene: Scenario, source: str) -> Dict[str, Optional[str]]:
    """The four digests the obligation names, read from one captured source."""
    fingerprints = scene.generate(source=source)["components"]["svc"]["fingerprints"]
    return {
        "exact": source_tree_digest(scene.root, "svc", source=source),
        "content_only": _content_only_digest(scene.root, "svc", source=source),
        "boundary": fingerprints["boundary"],
        "behavior": fingerprints["behavior"],
    }


class ModeRotationThroughTheIndexTests(unittest.TestCase):
    """OBL-GIT-SOURCE-064: identical bytes, three modes, four rotated digests."""

    @classmethod
    def setUpClass(cls):
        cls.observed: Dict[str, Dict[str, Dict[str, Optional[str]]]] = {}
        cls.recorded: Dict[str, Dict[str, str]] = {}
        cls.blobs: Dict[str, set] = {}
        cls.returned: Dict[str, Dict[str, Optional[str]]] = {}
        for source in CAPTURED_SOURCES:
            with _mode_scenario() as scene:
                oid = scene.git("rev-parse", "HEAD:svc/contract.txt")
                per_mode: Dict[str, Dict[str, Optional[str]]] = {}
                listing: Dict[str, str] = {}
                blobs = set()
                for mode in GIT_MODES:
                    scene.git(
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        f"{mode},{oid},svc/contract.txt",
                    )
                    scene.commit_index(f"mode {mode}")
                    listing[mode] = scene.git(
                        "ls-files", "-s", "svc/contract.txt"
                    ).split()[0]
                    blobs.add(scene.blob("svc/contract.txt"))
                    per_mode[mode] = _facets(scene, source)
                # Back to the first mode: the digests must come back with it.
                scene.git(
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"{GIT_MODES[0]},{oid},svc/contract.txt",
                )
                scene.commit_index("back to the first mode")
                cls.returned[source] = _facets(scene, source)
                cls.observed[source] = per_mode
                cls.recorded[source] = listing
                cls.blobs[source] = blobs

    def test_the_index_really_carries_each_mode_it_was_given(self):
        """The premise: without this, three equal digests would prove nothing."""
        for source in CAPTURED_SOURCES:
            for mode in GIT_MODES:
                with self.subTest(source=source, mode=mode):
                    self.assertEqual(self.recorded[source][mode], mode)

    def test_the_blob_bytes_never_moved(self):
        """The premise: only the mode is allowed to differ across the walk."""
        for source in CAPTURED_SOURCES:
            with self.subTest(source=source):
                self.assertEqual(self.blobs[source], {b"target"})

    def test_every_facet_was_actually_computed(self):
        """The premise for "three distinct": None three times is not a rotation."""
        for source in CAPTURED_SOURCES:
            for mode in GIT_MODES:
                for facet in ROTATING_FACETS:
                    with self.subTest(source=source, mode=mode, facet=facet):
                        self.assertIsInstance(
                            self.observed[source][mode][facet], str
                        )

    def test_each_mode_transition_rotates_all_four_digests(self):
        for source in CAPTURED_SOURCES:
            for facet in ROTATING_FACETS:
                with self.subTest(source=source, facet=facet):
                    values = [self.observed[source][mode][facet] for mode in GIT_MODES]
                    self.assertEqual(
                        len(set(values)),
                        len(GIT_MODES),
                        f"{facet} did not separate {GIT_MODES}: {values}",
                    )

    def test_returning_to_the_first_mode_returns_every_digest(self):
        """The contrast: commits do not move a digest, modes do.

        Four commits later, with the same bytes back under the same mode, all
        four digests are the ones the first commit produced. So the differences
        above are attributable to the mode and not to the walk that set it.
        """
        for source in CAPTURED_SOURCES:
            with self.subTest(source=source):
                self.assertEqual(
                    self.returned[source], self.observed[source][GIT_MODES[0]]
                )


#: Git object types a tree record may legitimately carry into a captured tree.
#: Everything else is refused by `_parse_ls_tree_record` before a digest sees it.
ACCEPTED_OBJECT_TYPES = ("blob", "commit")

#: Shapes `_parse_ls_tree_record` must refuse, keyed by the type they announce.
#: "tree" is the one Git itself emits when `-r` is absent; the others are here
#: so the refusal is a rule about the allow-list rather than one hard-coded name.
REFUSED_OBJECT_TYPES = ("tree", "tag", "")


class NonBlobTreeEntryTests(unittest.TestCase):
    """OBL-GIT-SOURCE-064: a non-blob entry fails closed, it does not hash empty."""

    OID = "a" * 40

    def _record(self, mode: str, object_type: str, path: str) -> bytes:
        header = f"{mode} {object_type} {self.OID}".encode("ascii")
        return header + b"\t" + path.encode("utf-8")

    def test_a_well_formed_blob_record_parses(self):
        """The premise: the parser answers, so a refusal below is a decision."""
        entry, path_bytes = _parse_ls_tree_record(
            self._record("100644", "blob", "svc/a.py")
        )
        self.assertEqual(entry.path, "svc/a.py")
        self.assertEqual(entry.mode, "100644")
        self.assertEqual(entry.object_type, "blob")
        self.assertEqual(entry.oid, self.OID)
        self.assertEqual(path_bytes, len("svc/a.py"))

    def test_both_accepted_object_types_reach_a_tree_entry(self):
        for object_type in ACCEPTED_OBJECT_TYPES:
            with self.subTest(object_type=object_type):
                mode = "100644" if object_type == "blob" else "160000"
                entry, _ = _parse_ls_tree_record(
                    self._record(mode, object_type, "svc/x")
                )
                self.assertEqual(entry.object_type, object_type)

    def test_every_other_object_type_is_refused(self):
        for object_type in REFUSED_OBJECT_TYPES:
            with self.subTest(object_type=object_type):
                with self.assertRaises(ValueError) as raised:
                    _parse_ls_tree_record(
                        self._record("040000", object_type, "svc")
                    )
                self.assertEqual(
                    str(raised.exception),
                    f"Unsupported Git object type {object_type!r} for 'svc'",
                )

    def test_the_collector_does_not_swallow_the_refusal(self):
        """One layer up: a bad record aborts the capture, it is not skipped."""
        with self.assertRaises(ValueError) as raised:
            _collect_ls_tree_entries(
                iter([self._record("040000", "tree", "svc")])
            )
        self.assertIn("Unsupported Git object type 'tree'", str(raised.exception))

    def test_git_really_emits_a_tree_record_without_the_recursion_flag(self):
        """The premise: the refused shape is Git's own output, not an invention.

        `_capture_tree_entries` always passes `-r`, so the refusal is
        defence in depth. It is worth having exactly because the shape is real:
        drop the flag and every directory arrives as a `tree` record.
        """
        with Scenario("treerecord") as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0" + LF)
            scene.commit()
            flat = [
                record
                for record in scene.git_bytes(
                    "ls-tree", "-z", "--full-tree", "HEAD"
                ).split(b"\x00")
                if record
            ]
            tree_records = [record for record in flat if b" tree " in record]
            self.assertTrue(tree_records, flat)
            with self.assertRaises(ValueError) as raised:
                _parse_ls_tree_record(tree_records[0])
            self.assertEqual(
                str(raised.exception),
                "Unsupported Git object type 'tree' for 'svc'",
            )
            recursed = [
                _parse_ls_tree_record(record)[0]
                for record in scene.git_bytes(
                    "ls-tree", "-r", "-z", "--full-tree", "HEAD"
                ).split(b"\x00")
                if record
            ]
            self.assertEqual(
                sorted(entry.path for entry in recursed),
                ["boundary.config.json", "svc/api/v1.yaml"],
            )
            self.assertEqual({entry.object_type for entry in recursed}, {"blob"})

    def test_a_gitlink_in_a_component_refuses_the_whole_generation(self):
        with Scenario("gitlink") as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/main.py", "x" + LF)
            scene.commit()
            oid = scene.submodule("svc/sub")
            scene.commit_index("add submodule")
            self.assertIn(
                f"160000 commit {oid}",
                scene.git("ls-tree", "-r", "HEAD"),
            )
            with self.assertRaises(ConfigError) as raised:
                scene.generate(source="head")
            self.assertIn(
                "Cannot hash non-blob Git entry at svc/sub: commit mode 160000",
                str(raised.exception),
            )

    def test_an_empty_blob_at_the_same_path_hashes_normally(self):
        """The contrast that makes "fails closed" mean what it says.

        Zero bytes are not what the refusal is about. The same path carrying an
        empty regular file produces an ordinary digest, so a regression that
        hashed the gitlink as empty bytes would produce a digest here too rather
        than an obvious failure.
        """
        with Scenario("emptyblob") as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/main.py", "x" + LF)
            scene.file("svc/sub", "")
            scene.commit()
            self.assertIn("svc/sub", scene.git("ls-tree", "-r", "HEAD"))
            self.assertEqual(scene.blob("svc/sub"), b"")
            digest = scene.generate(source="head")["components"]["svc"][
                "fingerprints"
            ]["exact"]
            self.assertIsInstance(digest, str)
            self.assertEqual(len(digest), 64)


# ---------------------------------------------------------------------------
# OBL-GLOBS-008: an unmatched declaration fails the provider
# ---------------------------------------------------------------------------

#: The declaration lists this obligation is about, as
#: ``label: (declared, surviving, message)``. Each `declared` pairs an entry
#: that selects a real file with one that selects nothing, so `selected` is
#: non-empty when the error is appended. `surviving` is the declaration that
#: selects exactly what `declared` would have hashed had the error been
#: swallowed, and its digest is the string the lock entry must never contain.
#: The literal and the glob spellings are separate rows because they take
#: different branches of `_resolve_declared_files` - and because they survive to
#: different file counts, one and two, they need different survivor digests.
MIXED_DECLARATIONS = {
    "literal": (
        ["api/openapi.yaml", "api/missing.yaml"],
        ["api/openapi.yaml"],
        "Declared boundary path matched no tracked files: api/missing.yaml",
    ),
    "glob": (
        ["api/*.yaml", "docs/*.md"],
        ["api/*.yaml"],
        "Declared boundary path matched no tracked files: docs/*.md",
    ),
}


def _selection_scenario(paths) -> Scenario:
    """A component with two tracked YAML files under `api/`."""
    scene = Scenario("selection")
    scene.component("svc", path="svc", provider="path-hash", boundary=list(paths))
    scene.file("svc/api/openapi.yaml", "openapi: 3.1.0" + LF)
    scene.file("svc/api/extra.yaml", "extra: true" + LF)
    scene.commit()
    return scene


def _boundary_context(scene: Scenario, accessor: _SourceAccessor) -> ProviderContext:
    return ProviderContext(
        repo_root=scene.root,
        component_path="svc",
        boundary_cfg=scene.config["components"]["svc"]["boundary"],
        source="head",
        read_file=accessor.read_file,
        read_file_limited=accessor.read_file_limited,
        list_files=accessor.list_files,
    )


class UnmatchedDeclarationTests(unittest.TestCase):
    """OBL-GLOBS-008: no digest over the surviving subset, ever."""

    @classmethod
    def setUpClass(cls):
        cls.survivor_digests: Dict[str, str] = {}
        for label, (_declared, surviving, _message) in MIXED_DECLARATIONS.items():
            with _selection_scenario(surviving) as scene:
                cls.survivor_digests[label] = scene.generate()["components"]["svc"][
                    "fingerprints"
                ]["boundary"]

    def test_each_row_has_a_survivor_digest_of_its_own(self):
        """The premise: the digest this obligation forbids is a reachable one.

        A different one per row, which is the whole reason the table carries a
        third column. The literal row survives to one file and the glob row to
        two, so a single shared survivor digest would be a string the glob row
        could not have contained even while publishing a forbidden digest of
        its own, and asserting its absence there would prove nothing.
        """
        for label, digest in self.survivor_digests.items():
            with self.subTest(declaration=label):
                self.assertIsInstance(digest, str)
                self.assertEqual(len(digest), 64)
        self.assertEqual(
            len(set(self.survivor_digests.values())),
            len(MIXED_DECLARATIONS),
            self.survivor_digests,
        )

    def test_the_survivors_are_selected_and_the_error_is_recorded_beside_them(self):
        """The branch that matters: `selected` is not empty when errors are.

        A fixture where nothing matched would exercise the "produced no digest"
        guard instead, and would still pass if the error check were weakened to
        `if errors and not selected`.

        This also ties the table's third column to the first: the declaration
        named as surviving selects exactly the files the failing declaration
        selected, so its digest really is the digest a swallowed error would
        publish, rather than a plausible-looking string of my own choosing.
        """
        for label, (declared, surviving, message) in MIXED_DECLARATIONS.items():
            with self.subTest(declaration=label):
                with _selection_scenario(declared) as scene:
                    with _SourceAccessor(scene.root, "head") as accessor:
                        ctx = _boundary_context(scene, accessor)
                        selected, errors = _resolve_declared_files(ctx, declared)
                        survivors, survivor_errors = _resolve_declared_files(
                            ctx, surviving
                        )
                self.assertTrue(selected, "nothing matched; the fixture is degenerate")
                self.assertEqual(errors, [message])
                self.assertEqual(survivor_errors, [])
                self.assertEqual(selected, survivors)

    def test_the_provider_fails_with_no_digest_at_all(self):
        for label, (paths, _surviving, message) in MIXED_DECLARATIONS.items():
            with self.subTest(declaration=label):
                with _selection_scenario(paths) as scene:
                    registry = create_registry()
                    with _SourceAccessor(scene.root, "head") as accessor:
                        digest, status, errors = compute_boundary(
                            registry["path-hash"], _boundary_context(scene, accessor)
                        )
                self.assertIsNone(digest)
                self.assertEqual(status, "error")
                self.assertEqual(errors, [message])

    def test_the_lock_entry_stores_the_error_and_never_the_subset_digest(self):
        for label, (paths, _surviving, message) in MIXED_DECLARATIONS.items():
            with self.subTest(declaration=label):
                with _selection_scenario(paths) as scene:
                    with _SourceAccessor(scene.root, "head") as accessor:
                        entry = _compute_component_entry(
                            "svc",
                            scene.config["components"]["svc"],
                            scene.root,
                            "head",
                            {},
                            accessor,
                            create_registry(),
                        )
                self.assertEqual(entry["boundary_status"], "error")
                self.assertIsNone(entry["fingerprints"]["boundary"])
                self.assertEqual(entry["boundary_errors"], [message])
                # By value, not by field, and this row's own survivor digest:
                # a weakened guard publishes exactly this string here, so the
                # assertion fires on both rows rather than on the literal one.
                self.assertNotIn(self.survivor_digests[label], json.dumps(entry))

    def test_no_generation_path_produces_a_lockfile(self):
        """Including --allow-partial, which relaxes slices and not this."""
        for label, (paths, _surviving, message) in MIXED_DECLARATIONS.items():
            with self.subTest(declaration=label):
                with _selection_scenario(paths) as scene:
                    for strict in (True, False):
                        with self.assertRaises(ConfigError) as raised:
                            generate_lockfile(
                                scene.config, scene.root, source="head", strict=strict
                            )
                        self.assertIn(f"svc: {message}", str(raised.exception))
                    for arguments in (("generate",), ("generate", "--allow-partial")):
                        result = run_cli(scene.root, *arguments)
                        self.assertEqual(result.returncode, 2, result.stderr)
                        self.assertIn(message, result.stderr)
                        self.assertFalse(
                            (scene.root / "boundary.lock.json").exists(),
                            f"{arguments} wrote a lockfile",
                        )

    def test_a_wholly_matching_list_writes_one(self):
        """The premise for the four assertions above: the CLI can succeed here."""
        with _selection_scenario(["api/openapi.yaml", "api/extra.yaml"]) as scene:
            result = run_cli(scene.root, "generate")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((scene.root / "boundary.lock.json").exists())


# ---------------------------------------------------------------------------
# OBL-GLOBS-009: deduplicated union, ordered by path bytes
# ---------------------------------------------------------------------------

#: Five spellings of one selection. Two of them put a literal inside the glob
#: that already covers it, in both orders; one is the glob alone; one names both
#: files literally; one overlaps two globs. All must produce one digest.
UNION_SPELLINGS = {
    "glob then literal": ["**/*.yaml", "api/openapi.yaml"],
    "literal then glob": ["api/openapi.yaml", "**/*.yaml"],
    "glob alone": ["**/*.yaml"],
    "both literals": ["api/openapi.yaml", "api/extra.yaml"],
    "two overlapping globs": ["**/*.yaml", "api/*.yaml"],
}

#: A pair of component-relative names whose UTF-8 bytes and code points sort
#: the other way round. "a\udcff" is what surrogateescape makes of a raw 0xff
#: filename byte, which encodes back to a single 0xff; "a\uffff" encodes to
#: 0xef 0xbf 0xbf. So bytes put \uffff first and code points put \udcff first.
ORDER_DISAGREEMENT = (
    "a" + chr(0xDCFF) + ".txt",
    "a" + chr(0xFFFF) + ".txt",
)


class DeclarationUnionTests(unittest.TestCase):
    """OBL-GLOBS-009: overlap costs nothing, order costs nothing, bytes decide."""

    def test_hashing_a_file_twice_would_move_the_digest(self):
        """The premise for "exactly one entry": duplication is observable."""
        entries = [("file:a.yaml", _ModeAwareBytes(b"x" + b"\n", "100644", "blob"))]
        once = _hash_framed_entries(entries, domain=HASH_DOMAIN_BOUNDARY)
        twice = _hash_framed_entries(entries * 2, domain=HASH_DOMAIN_BOUNDARY)
        self.assertNotEqual(once, twice)

    def test_every_declaration_spelling_produces_one_boundary_digest(self):
        def build() -> Scenario:
            return Scenario("union")

        def declare(paths):
            def apply(scene: Scenario) -> None:
                scene.component(
                    "svc", path="svc", provider="path-hash", boundary=list(paths)
                )
                scene.file("svc/api/openapi.yaml", "openapi: 3.1.0" + LF)
                scene.file("svc/api/extra.yaml", "extra: true" + LF)
                scene.commit()

            return apply

        results = assert_variants_agree(
            self,
            build,
            [(name, declare(paths)) for name, paths in UNION_SPELLINGS.items()],
            lambda scene: scene.generate()["components"]["svc"]["fingerprints"][
                "boundary"
            ],
            "declaration spelling",
        )
        for name, digest in results.items():
            with self.subTest(spelling=name):
                self.assertIsInstance(digest, str)

    def test_a_file_matched_twice_contributes_exactly_one_entry(self):
        registry = create_registry()
        for name, paths in UNION_SPELLINGS.items():
            with self.subTest(spelling=name):
                with _selection_scenario(paths) as scene:
                    with _SourceAccessor(scene.root, "head") as accessor:
                        resolved = registry["path-hash"].resolve(
                            _boundary_context(scene, accessor)
                        )
                labels = [label for label, _ in resolved.entries]
                self.assertEqual(
                    labels, ["file:api/extra.yaml", "file:api/openapi.yaml"]
                )

    def test_the_two_orderings_really_disagree_about_this_pair(self):
        """The premise: without a disagreeing pair the sort key is unobservable."""
        first, second = ORDER_DISAGREEMENT
        self.assertEqual(
            first.encode("utf-8", errors="surrogateescape"), b"a\xff.txt"
        )
        self.assertEqual(
            second.encode("utf-8", errors="surrogateescape"), b"a\xef\xbf\xbf.txt"
        )
        by_code_point = sorted(ORDER_DISAGREEMENT)
        by_bytes = sorted(
            ORDER_DISAGREEMENT,
            key=lambda value: value.encode("utf-8", errors="surrogateescape"),
        )
        self.assertEqual(by_code_point, [first, second])
        self.assertEqual(by_bytes, [second, first])
        self.assertNotEqual(by_code_point, by_bytes)

    def test_the_selection_is_ordered_by_path_bytes(self):
        """Driven through a synthetic listing: no filesystem here holds these.

        Windows stores filenames as UTF-16 and has no byte a decoder must
        surrogate-escape, so the disagreeing pair cannot be committed on this
        host. `list_files` is the seam boundver already uses to read a tree, so
        handing it the pair exercises the real sort key in `_resolve_declared_files`.
        """
        first, second = ORDER_DISAGREEMENT
        files = {
            f"svc/{first}": _ModeAwareBytes(b"A" + b"\n", "100644", "blob"),
            f"svc/{second}": _ModeAwareBytes(b"B" + b"\n", "100644", "blob"),
        }

        def list_files(prefix: str) -> List[str]:
            trimmed = prefix.rstrip("/")
            return sorted(
                name
                for name in files
                if name == trimmed or name.startswith(trimmed + "/")
            )

        ctx = ProviderContext(
            repo_root=Path("."),
            component_path="svc",
            boundary_cfg={"provider": "path-hash", "paths": ["*.txt"]},
            source="head",
            read_file=files.__getitem__,
            list_files=list_files,
        )
        selected, errors = _resolve_declared_files(ctx, ["*.txt"])
        self.assertEqual(errors, [])
        self.assertEqual([relative for _, relative in selected], [second, first])
        resolved = create_registry()["path-hash"].resolve(ctx)
        self.assertEqual(
            [label for label, _ in resolved.entries],
            [f"file:{second}", f"file:{first}"],
        )


# ---------------------------------------------------------------------------
# OBL-GLOBS-011: the schema pattern against the Python normalizer
# ---------------------------------------------------------------------------

PACKAGED_SCHEMA = Path(boundver.__file__).resolve().parent / "boundary.config.schema.json"
SCHEMA_DOCUMENT = json.loads(PACKAGED_SCHEMA.read_text(encoding="utf-8"))
RELATIVE_PATH_SCHEMA = SCHEMA_DOCUMENT["$defs"]["relativePath"]
RELATIVE_PATH_VALIDATOR = jsonschema.Draft202012Validator(RELATIVE_PATH_SCHEMA)

#: The spellings the obligation lists by name, and nothing else. Every one of
#: them gets the same verdict from both layers, which is the claim under test.
NAMED_PATH_SPELLINGS = (
    "api/v1.yaml",
    "api/",
    "/abs/path",
    "C:/x",
    "a" + BACKSLASH + "b",
    "./api.yaml",
    "a/./b",
    "a/../b",
    "..",
    ".",
    "a//b",
    "",
    " api.yaml",
    "api.yaml ",
    " ",
)

#: One witness per class where the two layers disagree, as
#: ``label: (value, schema_accepts, normalizer_accepts, normalizer_reason)``.
#: The reason is the exact ValueError text, and is None for the two classes
#: where the schema is the stricter side. Four are ceilings; the obligation
#: names three of them - 16 KiB, 1,024 segments, 256 metacharacters - and
#: concedes the pattern cannot express them. Runtime-only ceilings remain
#: explicit because JSON Schema cannot express UTF-8 byte or per-segment work
#: budgets without rejecting otherwise valid paths.
PATH_DIVERGENCES = {
    "declared byte limit": (
        "a" * (MAX_DECLARED_PATH_BYTES + 1),
        True,
        False,
        f"must not exceed {MAX_DECLARED_PATH_BYTES} UTF-8 bytes",
    ),
    "path segment limit": (
        "/".join(["a"] * (MAX_GLOB_SEGMENTS + 1)),
        True,
        False,
        f"must not exceed {MAX_GLOB_SEGMENTS} path segments",
    ),
    "glob metacharacter limit": (
        "*" * (MAX_GLOB_METACHARACTERS_PER_SEGMENT + 1),
        True,
        False,
        "glob segments must not contain more than "
        f"{MAX_GLOB_METACHARACTERS_PER_SEGMENT} wildcard metacharacters",
    ),
    "glob segment byte limit": (
        "*" + "a" * MAX_GLOB_PATTERN_SEGMENT_BYTES,
        True,
        False,
        "glob segments must not exceed "
        f"{MAX_GLOB_PATTERN_SEGMENT_BYTES} UTF-8 bytes",
    ),
}

#: The alphabet the search runs over: both separators, the dot and colon the
#: schema special-cases, a backslash, the wildcard metacharacters, a space, and
#: three line terminators. A character absent here cannot be found by the search.
PATH_ALPHABET = "ab/.:" + BACKSLASH + " *?[" + LF + chr(13) + chr(9)


def schema_accepts_path(value: str) -> bool:
    return RELATIVE_PATH_VALIDATOR.is_valid(value)


def normalizer_accepts_path(value: str) -> bool:
    try:
        _normalize_declared_path(value)
    except ValueError:
        return False
    return True


def schema_only_refusal(value: str) -> Optional[str]:
    """Why the pattern refuses something the normalizer would take."""
    return None


def python_only_refusal(value: str) -> Optional[str]:
    """Why the normalizer refuses something the pattern would take.

    This mirrors the ceilings rather than deriving them, and is used only to
    decide which strings the equality claim below is allowed to skip. A widened
    ceiling makes it skip a string both layers now accept, which cannot mask a
    failure.
    """
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return "lone surrogate"
    if len(encoded) > MAX_DECLARED_PATH_BYTES:
        return "declared byte limit"
    segments = value.rstrip("/").split("/")
    if len(segments) > MAX_GLOB_SEGMENTS:
        return "path segment limit"
    for segment in segments:
        if not _is_glob(segment):
            continue
        if len(segment.encode("utf-8")) > MAX_GLOB_PATTERN_SEGMENT_BYTES:
            return "glob segment byte limit"
        if sum(character in "*?[" for character in segment) > (
            MAX_GLOB_METACHARACTERS_PER_SEGMENT
        ):
            return "glob metacharacter limit"
    return None


class RelativePathLayerAgreementTests(unittest.TestCase):
    """OBL-GLOBS-011: one verdict per string, from the schema and from Python."""

    def test_the_schema_read_here_is_the_one_that_ships(self):
        """The premise: a differential against a stale copy proves nothing."""
        self.assertTrue(PACKAGED_SCHEMA.is_file(), PACKAGED_SCHEMA)
        root_copy = Path(__file__).resolve().parents[1] / "boundary.config.schema.json"
        self.assertEqual(
            root_copy.read_bytes(), PACKAGED_SCHEMA.read_bytes(),
            "the published schema and the packaged schema have diverged",
        )
        self.assertEqual(RELATIVE_PATH_SCHEMA["minLength"], 1)
        self.assertNotIn(
            "maxLength", RELATIVE_PATH_SCHEMA,
            "a maxLength would remove the byte-limit divergence below",
        )

    def test_both_layers_answer_both_ways(self):
        """The premise: neither function is a constant."""
        self.assertTrue(schema_accepts_path("api/v1.yaml"))
        self.assertFalse(schema_accepts_path("/abs/path"))
        self.assertTrue(normalizer_accepts_path("api/v1.yaml"))
        self.assertFalse(normalizer_accepts_path("/abs/path"))

    def test_every_spelling_the_obligation_names_gets_one_verdict(self):
        for value in NAMED_PATH_SPELLINGS:
            with self.subTest(value=repr(value)):
                self.assertEqual(
                    schema_accepts_path(value),
                    normalizer_accepts_path(value),
                    f"{value!r}: schema={schema_accepts_path(value)} "
                    f"normalizer={normalizer_accepts_path(value)}",
                )

    @PROFILE
    @given(
        value=st.lists(
            st.sampled_from(PATH_ALPHABET), min_size=1, max_size=8
        ).map("".join)
    )
    def test_no_eighth_divergence_class_exists(self, value):
        if schema_only_refusal(value) or python_only_refusal(value):
            return
        self.assertEqual(
            schema_accepts_path(value),
            normalizer_accepts_path(value),
            f"{value!r}: schema={schema_accepts_path(value)} "
            f"normalizer={normalizer_accepts_path(value)}",
        )

    def test_each_known_divergence_still_behaves_exactly_as_recorded(self):
        for label, (value, schema, python, reason) in PATH_DIVERGENCES.items():
            with self.subTest(divergence=label):
                self.assertEqual(schema_accepts_path(value), schema)
                self.assertEqual(normalizer_accepts_path(value), python)
                if reason is not None:
                    with self.assertRaises(ValueError) as raised:
                        _normalize_declared_path(value)
                    self.assertEqual(str(raised.exception), reason)

    def test_each_divergence_class_is_classified_by_exactly_one_side(self):
        """A witness must be explained, and by the side that actually refuses."""
        for label, (value, schema, python, _reason) in PATH_DIVERGENCES.items():
            with self.subTest(divergence=label):
                if schema and not python:
                    self.assertEqual(python_only_refusal(value), label)
                    self.assertIsNone(schema_only_refusal(value))
                else:
                    self.assertEqual(schema_only_refusal(value), label)
                    self.assertIsNone(python_only_refusal(value))

    def test_only_explicit_runtime_ceilings_differ_between_the_layers(self):
        for label, (value, schema, python, reason) in PATH_DIVERGENCES.items():
            with self.subTest(ceiling=label):
                self.assertTrue(schema)
                self.assertFalse(python)
                self.assertTrue(schema_accepts_path(value))
                self.assertFalse(normalizer_accepts_path(value))
                self.assertIsNotNone(reason)

    @settings(max_examples=40, deadline=None)
    @given(
        count=st.integers(
            min_value=MAX_GLOB_SEGMENTS - 2, max_value=MAX_GLOB_SEGMENTS + 2
        )
    )
    def test_the_segment_ceiling_is_python_only(self, count):
        value = "/".join(["a"] * count)
        self.assertTrue(schema_accepts_path(value))
        self.assertEqual(normalizer_accepts_path(value), count <= MAX_GLOB_SEGMENTS)

    @settings(max_examples=40, deadline=None)
    @given(
        size=st.integers(
            min_value=MAX_DECLARED_PATH_BYTES - 2, max_value=MAX_DECLARED_PATH_BYTES + 2
        )
    )
    def test_the_byte_ceiling_is_python_only(self, size):
        value = "a" * size
        self.assertTrue(schema_accepts_path(value))
        self.assertEqual(
            normalizer_accepts_path(value), size <= MAX_DECLARED_PATH_BYTES
        )

    @settings(max_examples=40, deadline=None)
    @given(
        count=st.integers(
            min_value=MAX_GLOB_METACHARACTERS_PER_SEGMENT - 2,
            max_value=MAX_GLOB_METACHARACTERS_PER_SEGMENT + 2,
        )
    )
    def test_the_metacharacter_ceiling_is_python_only(self, count):
        value = "*" * count
        self.assertTrue(schema_accepts_path(value))
        self.assertEqual(
            normalizer_accepts_path(value),
            count <= MAX_GLOB_METACHARACTERS_PER_SEGMENT,
        )


# ---------------------------------------------------------------------------
# OBL-GLOBS-012: the component-identifier grammar, code point by code point
# ---------------------------------------------------------------------------

COMPONENT_IDENTIFIER_SCHEMA = SCHEMA_DOCUMENT["$defs"]["componentIdentifier"]
COMPONENT_IDENTIFIER_VALIDATOR = jsonschema.Draft202012Validator(
    COMPONENT_IDENTIFIER_SCHEMA
)
COMPONENT_IDENTIFIER_RE = re.compile(COMPONENT_IDENTIFIER_SCHEMA["pattern"])

#: The code points the schema pattern enumerates by hand, expanded. The claim
#: in _config_contract.py is that this is exactly CPython's `str.strip` set, and
#: the test below checks that against `str.strip` itself rather than trusting it.
PATTERN_WHITESPACE = frozenset(
    list(range(0x09, 0x0E))
    + list(range(0x1C, 0x21))
    + [0x85, 0xA0, 0x1680]
    + list(range(0x2000, 0x200B))
    + [0x2028, 0x2029, 0x202F, 0x205F, 0x3000]
)

#: Names carrying the delimiter, which neither layer may accept wherever it sits.
COMMA_NAMES = (",", "a,b", ",a", "a,", "a,,b")


class ComponentIdentifierGrammarTests(unittest.TestCase):
    """OBL-GLOBS-012: every code point, both layers, one answer."""

    @classmethod
    def setUpClass(cls):
        cls.disagreements: List[tuple] = []
        cls.schema_rejected_leading: set = set()
        cls.schema_rejected_trailing: set = set()
        cls.accepted = 0
        for code in range(0x110000):
            character = chr(code)
            for position, name in (
                ("leading", character + "x"),
                ("trailing", "x" + character),
            ):
                schema_ok = COMPONENT_IDENTIFIER_RE.search(name) is not None
                python_ok = component_identifier_problem(name) is None
                if schema_ok != python_ok:
                    cls.disagreements.append((hex(code), position, schema_ok, python_ok))
                if schema_ok:
                    cls.accepted += 1
                elif position == "leading":
                    cls.schema_rejected_leading.add(code)
                else:
                    cls.schema_rejected_trailing.add(code)

    def test_the_pattern_under_test_is_the_shipped_one(self):
        """The premise: the enumeration is about the artifact, not a copy."""
        self.assertEqual(
            COMPONENT_IDENTIFIER_SCHEMA["pattern"], COMPONENT_IDENTIFIER_PATTERN
        )
        self.assertEqual(
            COMPONENT_IDENTIFIER_SCHEMA["maxLength"], MAX_CONSUMER_IDENTIFIER_CHARS
        )

    def test_the_compiled_pattern_is_what_the_validator_applies(self):
        """The premise for enumerating with `re` instead of the validator.

        Draft202012Validator applies `pattern` with `re.search`, so the two
        agree by construction; 2.2 million validator calls would cost minutes
        where the compiled pattern costs seconds. Checked on a sample that
        spans the ASCII range, every whitespace code point the pattern names,
        and both ends of the astral plane.
        """
        sample = list(range(0, 0x400)) + sorted(PATTERN_WHITESPACE) + [
            0x10000,
            0x1F600,
            0x10FFFF,
        ]
        for code in sample:
            for name in (chr(code) + "x", "x" + chr(code)):
                with self.subTest(code=hex(code), name=repr(name)):
                    self.assertEqual(
                        COMPONENT_IDENTIFIER_VALIDATOR.is_valid(name),
                        COMPONENT_IDENTIFIER_RE.search(name) is not None,
                    )

    def test_the_enumeration_saw_both_verdicts(self):
        """The premise: a pattern that accepted everything would disagree nowhere."""
        self.assertGreater(self.accepted, 2_000_000)
        self.assertTrue(self.schema_rejected_leading)
        self.assertTrue(self.schema_rejected_trailing)

    def test_the_two_layers_agree_on_every_unicode_code_point(self):
        self.assertEqual(self.disagreements, [], self.disagreements[:10])

    def test_the_pattern_rejects_exactly_what_strip_removes_plus_the_delimiter(self):
        """The oracle is `str.strip` itself, not the enumerated ranges."""
        stripped = {
            code
            for code in range(0x110000)
            if (chr(code) + "x").strip() != chr(code) + "x"
        }
        expected = stripped | {ord(",")}
        self.assertEqual(self.schema_rejected_leading, expected)
        self.assertEqual(self.schema_rejected_trailing, expected)

    def test_the_hand_written_ranges_are_cpythons_strip_set(self):
        """If CPython gains a whitespace code point, this is what notices."""
        stripped = {
            code
            for code in range(0x110000)
            if (chr(code) + "x").strip() != chr(code) + "x"
        }
        self.assertEqual(stripped, set(PATTERN_WHITESPACE))
        self.assertEqual(len(stripped), 29)

    def test_the_delimiter_is_refused_wherever_it_sits(self):
        for name in COMMA_NAMES:
            with self.subTest(name=name):
                self.assertFalse(COMPONENT_IDENTIFIER_VALIDATOR.is_valid(name))
                self.assertEqual(
                    component_identifier_problem(name),
                    "must not contain ',' because CLI, GitHub Action, and GitLab "
                    "component filters are comma-separated",
                )

    def test_an_astral_name_is_ordinary_to_both_layers(self):
        """The premise for the length rows below: astral names are legal names."""
        for name in ("\U0001f600", "a\U0001f600b", "\U0010ffff"):
            with self.subTest(name=repr(name)):
                self.assertTrue(COMPONENT_IDENTIFIER_VALIDATOR.is_valid(name))
                self.assertIsNone(component_identifier_problem(name))

    def test_the_length_ceiling_is_counted_in_code_points_by_both_layers(self):
        """Including across the astral boundary, where the units could differ.

        A name of 16,384 astral characters is 32,768 UTF-16 code units and
        65,536 UTF-8 bytes. Both layers here count code points and agree. A
        JSON-Schema engine whose strings are UTF-16, which is what ECMA-262
        `String.length` gives, would count 32,768 and refuse a name boundver
        accepts; that engine is not reachable from this suite, so what is
        pinned below is the count Python's validator performs.
        """
        limit = MAX_CONSUMER_IDENTIFIER_CHARS
        for character, label in (("a", "bmp"), ("\U0001f600", "astral")):
            for length, accepted in ((limit, True), (limit + 1, False)):
                with self.subTest(plane=label, length=length):
                    name = character * length
                    self.assertEqual(
                        COMPONENT_IDENTIFIER_VALIDATOR.is_valid(name), accepted
                    )
                    self.assertEqual(
                        component_identifier_problem(name) is None, accepted
                    )
                    if not accepted:
                        self.assertEqual(
                            component_identifier_problem(name),
                            f"exceeds the {limit}-character limit",
                        )
        self.assertEqual(len(("\U0001f600" * limit).encode("utf-16-le")) // 2, 2 * limit)


# ---------------------------------------------------------------------------
# OBL-GLOBS-025: RFC 6901 escaping in structural-diff pointers
# ---------------------------------------------------------------------------

#: The escapes the obligation names, plus the two compositions that separate
#: the correct order from the reversed one. Every value is a JSON object key.
POINTER_ESCAPES = {
    "a/b": "/a~1b",
    "a~b": "/a~0b",
    "~1": "/~01",
    "~0": "/~00",
    "a~/b": "/a~0~1b",
    "plain": "/plain",
    "": "/",
    "~": "/~0",
    "/": "/~1",
}

#: The pair a missing tilde escape collapses onto one pointer. Both would
#: become "/~1", so a structural change under "~1" would be reported at "/".
COLLIDING_KEYS = ("~1", "/")

#: The six characters every collision the escaping prevents is spelled with:
#: both escape markers, both digits they can be followed by, the separator, and
#: two ordinary letters so a witness can carry context around them. Drawn from
#: all of Unicode instead, two keys differ and so do their pointers, and an
#: injectivity claim over them is satisfied without touching the escaping.
POINTER_COLLISION_ALPHABET = "~01/ab"

#: How far the exhaustive sweep below runs, and what it found there. Recorded
#: rather than recomputed so a silent change in either number is a failure.
POINTER_SWEEP_LENGTH = 3
POINTER_SWEEP_KEYS = 259
POINTER_SWEEP_FUSED_GROUPS = 12
POINTER_SWEEP_FUSED_KEYS = 25


def pointer_without_the_tilde_escape(key: str) -> str:
    """`pointer_child` as it reads with `.replace("~", "~0")` deleted.

    A local model of the mutant, written here so a test can demonstrate that a
    pair of keys really does collide under it before asserting that the shipped
    `pointer_child` keeps that pair apart.
    """
    return "/" + str(key).replace("/", "~1")


def _short_keys_over_the_collision_alphabet() -> List[str]:
    """Every string of at most POINTER_SWEEP_LENGTH characters, empty included."""
    return [
        "".join(letters)
        for length in range(POINTER_SWEEP_LENGTH + 1)
        for letters in itertools.product(POINTER_COLLISION_ALPHABET, repeat=length)
    ]


def unescape_pointer_segment(segment: str) -> str:
    """RFC 6901 unescaping: ~1 becomes /, then ~0 becomes ~.

    The inverse of the operation under test, in the order the RFC gives for it,
    written here rather than imported so the round trip is not the code under
    test checking itself.
    """
    return segment.replace("~1", "/").replace("~0", "~")


def _entries(value) -> List[tuple]:
    return [("contract.json", json.dumps(value, sort_keys=True).encode("utf-8"))]


def _changes(before, after) -> List[tuple]:
    result = diff_canonical_json_entries(
        _entries(before), _entries(after), StructuralDiffBudget()
    )
    return [
        (change.kind, change.path)
        for document in result.documents
        for change in document.changes
    ]


class JsonPointerEscapingTests(unittest.TestCase):
    """OBL-GLOBS-025: a pointer names the member that changed, and only it."""

    def test_each_named_key_escapes_to_the_recorded_pointer(self):
        budget = StructuralDiffBudget()
        for key, pointer in POINTER_ESCAPES.items():
            with self.subTest(key=repr(key)):
                self.assertEqual(budget.pointer_child("", key), pointer)

    def test_reversing_the_two_replacements_is_visible(self):
        """The premise: the assertions above can fail.

        Replacing "/" before "~" turns "a/b" into "a~1b" and then mangles the
        tilde it just wrote, giving "/a~01b" - which is still a well-formed
        pointer and still unescapes, just to the wrong key.
        """
        reversed_order = "/" + "a/b".replace("/", "~1").replace("~", "~0")
        self.assertEqual(reversed_order, "/a~01b")
        self.assertNotEqual(reversed_order, POINTER_ESCAPES["a/b"])
        self.assertTrue(_is_json_pointer(reversed_order))
        self.assertNotEqual(unescape_pointer_segment(reversed_order[1:]), "a/b")

    def test_dropping_the_tilde_escape_would_collide_two_keys(self):
        """The premise for injectivity: the collision is a reachable one."""
        without = {key: "/" + key.replace("/", "~1") for key in COLLIDING_KEYS}
        self.assertEqual(set(without.values()), {"/~1"})
        budget = StructuralDiffBudget()
        actual = {key: budget.pointer_child("", key) for key in COLLIDING_KEYS}
        self.assertEqual(len(set(actual.values())), 2, actual)

    def test_every_named_pointer_is_a_pointer_and_recovers_its_key(self):
        for key, pointer in POINTER_ESCAPES.items():
            with self.subTest(key=repr(key)):
                self.assertTrue(_is_json_pointer(pointer))
                self.assertEqual(unescape_pointer_segment(pointer[1:]), key)

    @PROFILE
    @given(key=st.text(max_size=40))
    def test_any_key_escapes_to_a_pointer_that_unescapes_back(self, key):
        pointer = StructuralDiffBudget().pointer_child("", key)
        self.assertTrue(pointer.startswith("/"))
        self.assertTrue(_is_json_pointer(pointer), pointer)
        self.assertEqual(unescape_pointer_segment(pointer[1:]), key)

    def test_the_collision_alphabet_is_where_injectivity_is_decidable(self):
        """The premise for both tests below: this alphabet has collisions in it.

        Over every string of at most three characters drawn from it,
        `pointer_child` hands out one pointer per key, while the same map with
        the tilde escape deleted fuses 25 of those keys onto 12 shared
        pointers. An alphabet without those fusions - which is what all of
        Unicode is, in practice, for two independently drawn strings - gives an
        injectivity claim nothing to fail on.
        """
        keys = _short_keys_over_the_collision_alphabet()
        self.assertEqual(len(keys), POINTER_SWEEP_KEYS)
        self.assertEqual(len(set(keys)), POINTER_SWEEP_KEYS)
        fused: Dict[str, List[str]] = {}
        for key in keys:
            fused.setdefault(pointer_without_the_tilde_escape(key), []).append(key)
        groups = [group for group in fused.values() if len(group) > 1]
        self.assertEqual(len(groups), POINTER_SWEEP_FUSED_GROUPS)
        self.assertEqual(
            sum(len(group) for group in groups), POINTER_SWEEP_FUSED_KEYS
        )
        self.assertIn(sorted(COLLIDING_KEYS), sorted(sorted(g) for g in groups))

    def test_no_two_short_keys_over_that_alphabet_share_a_pointer(self):
        """The deterministic half: 259 keys, 259 pointers, no luck involved."""
        budget = StructuralDiffBudget()
        seen: Dict[str, str] = {}
        for key in _short_keys_over_the_collision_alphabet():
            pointer = budget.pointer_child("", key)
            self.assertNotIn(
                pointer,
                seen,
                f"{key!r} and {seen.get(pointer)!r} both point at {pointer!r}",
            )
            seen[pointer] = key
        self.assertEqual(len(seen), POINTER_SWEEP_KEYS)

    @PROFILE
    @given(
        before=st.text(alphabet=POINTER_COLLISION_ALPHABET, max_size=4),
        after=st.text(alphabet=POINTER_COLLISION_ALPHABET, max_size=4),
    )
    def test_distinct_keys_never_share_a_pointer(self, before, after):
        """Each key is drawn beside the key a dropped tilde escape would fuse it to.

        Two independently drawn `st.text()` strings differ, and so do their
        pointers, so the biconditional below would hold for a reason that has
        nothing to do with escaping - and it did: deleting `.replace("~", "~0")`
        from `pointer_child` turned nineteen tests in this class red and left
        that spelling green. The twin is constructed instead. Every drawn key
        contains "~1", so its twin is a shorter and therefore different key, and
        the first assertion proves the two share a pointer once the tilde
        escape is gone. What remains for `pointer_child` to get right is
        separating them, on every example rather than on a lucky one.
        """
        key = before + "~1" + after
        twin = key.replace("~1", "/")
        self.assertNotEqual(key, twin)
        self.assertEqual(
            pointer_without_the_tilde_escape(key),
            pointer_without_the_tilde_escape(twin),
        )
        budget = StructuralDiffBudget()
        pointers = (budget.pointer_child("", key), budget.pointer_child("", twin))
        self.assertEqual(key == twin, pointers[0] == pointers[1])
        self.assertEqual(unescape_pointer_segment(pointers[0][1:]), key)
        self.assertEqual(unescape_pointer_segment(pointers[1][1:]), twin)

    @PROFILE
    @given(
        parent=st.text(alphabet="ab/~", max_size=6),
        child=st.text(alphabet="ab/~", max_size=6),
    )
    def test_a_nested_pointer_splits_back_into_its_two_keys(self, parent, child):
        budget = StructuralDiffBudget()
        pointer = budget.pointer_child(budget.pointer_child("", parent), child)
        self.assertTrue(_is_json_pointer(pointer), pointer)
        segments = pointer.split("/")[1:]
        self.assertEqual(len(segments), 2, pointer)
        self.assertEqual(
            [unescape_pointer_segment(segment) for segment in segments],
            [parent, child],
        )

    def test_an_unchanged_document_emits_no_pointer_at_all(self):
        """The premise: the pointers below come from real changes."""
        result = diff_canonical_json_entries(
            _entries({"a~b": 1}), _entries({"a~b": 1}), StructuralDiffBudget()
        )
        self.assertEqual(result.documents, ())

    def test_the_diff_reports_a_changed_value_at_its_escaped_pointer(self):
        for key, pointer in POINTER_ESCAPES.items():
            with self.subTest(key=repr(key)):
                self.assertEqual(_changes({key: 1}, {key: 2}), [("changed", pointer)])

    def test_added_and_removed_keys_carry_the_same_escaping(self):
        self.assertEqual(
            _changes({"a~b": 1}, {"a/b": 1}),
            [("added", "/a~1b"), ("removed", "/a~0b")],
        )

    def test_a_nested_object_and_an_array_index_compose(self):
        self.assertEqual(
            _changes({"a~b": {"c/d": [1]}}, {"a~b": {"c/d": ["x"]}}),
            [("changed", "/a~0b/c~1d/0")],
        )

    def test_every_pointer_a_diff_emits_satisfies_the_validator(self):
        keys = list(POINTER_ESCAPES)
        before = {key: index for index, key in enumerate(keys)}
        after = {key: index + 1 for index, key in enumerate(keys)}
        emitted = [path for _kind, path in _changes(before, after)]
        self.assertEqual(len(emitted), len(keys))
        for pointer in emitted:
            with self.subTest(pointer=pointer):
                self.assertTrue(_is_json_pointer(pointer), pointer)
        self.assertEqual(
            sorted(unescape_pointer_segment(pointer[1:]) for pointer in emitted),
            sorted(keys),
        )


if __name__ == "__main__":
    unittest.main()
