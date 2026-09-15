"""Two readers for one version, and an audit report that must add up.

`extract_version` has two implementations of a single contract. Every command
hands it an accessor bound to a captured Git source; nothing inside boundver
ever reaches the disk fallback underneath. That fallback is nevertheless
public: `boundver.core` re-exports it, together with `_extract_json_field`,
`_extract_toml_field` and `_extract_yaml_field`, as deliberate `as`-style
aliases, so an embedder who imports it gets a reader with a materially weaker
policy than the one the tool applies to itself. The interesting question is
therefore not whether either branch reads a version — both do — but whether
they refuse in the same way for the same reason, and the register claims they
do not. Settling that needed a differential harness rather than another
happy-path read: one that runs a declaration through the disk branch and
through all three accessors and reports the partition, so a failure names which
readers agreed with which instead of dumping two values. `tests/_parity.py`
already had exactly that reporting, so this file borrows it. The differential's
two halves are separated on purpose. Which declarations carry a value is a
small finite matrix, so it is pinned exhaustively by table; the generated
property is aimed at the much larger space of declarations that resolve to
nothing, where the whole question is whether one reader returns `None` while
another raises.

The claimed divergences were harder to reach than they look. This host refuses
symlink creation, so the working-tree symlink read-through could not be
exercised at all; what could be exercised is the same gate one layer up, by
writing a mode-120000 index entry with `update-index --cacheinfo`, which needs
no privilege. That reaches the symlink refusal on the head and index readers
only — the entry puts nothing on disk, so the disk fallback stops at
`full_path.exists()` and the working-tree reader stops at a read of a file that
is not there. Both of those rows are pinned against the absent-file answers so
it is visible which reader reached the gate the test is named for and which did
not. The mid-read race needed no race: an absent file already yields `None` on
disk and an escaping `ValueError` through every accessor, which is the same
asymmetry the register describes. `_read_version_file_bytes` — named by no test
until now — is pinned twice, once against exception instances at runtime and
once against its own `except` clause read with `ast`, because `ConfigError` and
`GuardrailError` are both `ValueError` subclasses and three of the four runtime
rows therefore land on one arm. The size ceiling is the one place where a real
ten-megabyte file was unavoidable: the existing test patches the limit to four
bytes and feeds five from a mock, so it exercises the second-line re-check in
`_extract_field_from_bytes` and neither real reader's pre-read gate, and an
off-by-one on the true boundary would survive it.

The migration analyser is the second half, and its "current" column is a
narrower independent claim than it first appears. For a literal selector the
analyser runs `_literal_matches`, a genuine reimplementation of the
`is_dir_like or is_selected_directory` branch of `_expand_component_paths`; for
a glob it runs `_match_path_glob`, which shares `_compile_path_glob_with_spender`
with the production matcher and differs only in its work-budget wrapper. So the
selectors are drawn as two properties rather than one: the literal draw is
weighted onto paths that exist, because that is where two implementations
really meet, and the glob draw earns its place through the *legacy* column,
which is a separate matcher whose disagreement with the production selection is
checked on every example. The provider is drawn alongside, because a config
that only ever says "openapi" produces nothing but comparable declarations and
leaves the whole uncompared branch of the oracle unexecuted. The work
accounting is instrumented by discovering the charge sites with `ast`, not by
listing them: `spend_evaluations` is a closure and cannot be patched, but every
call that receives it is a module-level function, so parsing
`analyze_selector_migration` finds them and a charge site added tomorrow is
instrumented without anyone editing this file.

Covers OBL-GIT-SOURCE-137, OBL-GIT-SOURCE-138, OBL-GIT-SOURCE-139,
OBL-GLOBS-022, OBL-GLOBS-024 and OBL-GLOBS-051.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
import unittest
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple
from unittest import mock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from boundver import _migration_analysis as migration
from boundver import versions
from boundver._config import _expand_component_paths
from boundver._git import MAX_GIT_BLOB_BYTES, _capture_git_source_snapshot
from boundver._hashing import MAX_HASH_FILE_BYTES
from boundver._lockfile import _SourceAccessor
from boundver._utils import ConfigError, GuardrailError
from boundver.versions import (
    MAX_VERSION_FILE_BYTES,
    _read_version_file_bytes,
    extract_version,
)

from tests._parity import describe, partition, run_cli, run_cli_in_process
from tests._scenarios import SOURCE_MODES, Scenario

PROFILE = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow],
)

#: Every example rewrites a file on disk and reads it back through two
#: readers. No Git subprocess runs per example, but the reads are still real
#: I/O, so the count is set to keep the class inside a few seconds.
IO_PROFILE = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow],
)


def _readings(
    repo_root: Path,
    component_path: str,
    version_source: dict,
    accessors: Dict[str, _SourceAccessor],
) -> Dict[str, Any]:
    """What every reader made of one declaration, exceptions included.

    An escaping exception is recorded as text rather than propagated, because
    the whole point of the differential is that one branch raises where another
    returns a value: letting it propagate would erase the comparison.
    """
    results: Dict[str, Any] = {}
    try:
        results["disk"] = extract_version(repo_root, component_path, version_source)
    except BaseException as exc:  # noqa: BLE001 - the raise is the observation
        results["disk"] = f"RAISED {type(exc).__name__}: {exc}"
    for name, accessor in accessors.items():
        try:
            results[name] = extract_version(
                repo_root,
                component_path,
                version_source,
                accessor.latest_tag,
                read_file_fn=accessor.version_read_file,
            )
        except BaseException as exc:  # noqa: BLE001 - the raise is the observation
            results[name] = f"RAISED {type(exc).__name__}: {exc}"
    return results


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-137: the accessor path and the disk fallback are one contract
# ---------------------------------------------------------------------------

#: Component roots the configuration layer accepts, spelled canonically. The
#: root component is included because both branches special-case it, by
#: different code: the disk branch joins ``repo_root / "." / file`` and the
#: accessor branch tests ``component_prefix in {"", "."}``.
CANONICAL_ROOTS = (".", "svc", "a/b", "x/y/z")

#: One document per interesting outcome of the shared byte-to-value step, so
#: the drawn (root, file, field) triple ranges over success, a missing field, a
#: parse failure and a value of the wrong type. The readers cannot tell these
#: apart — that is the claim — so any disagreement is the reader's.
VERSION_DOCUMENTS = {
    "package.json": '{"version": "1.2.3", "meta": {"tag": "v9"}}\n',
    "pyproject.toml": 'version = "2.0.0"\n\n[tool]\nrelease = "3.0.0"\n',
    "meta.yaml": "version: 4.0.0\nmeta:\n  tag: v5\n",
    "info.yml": "version: 6.0.0\n",
    "broken.json": "{not json\n",
    "wrong.toml": "version = 7\n",
}

#: Field paths that resolve, that stop short, and that walk into a scalar.
KNOWN_FIELD_PATHS = (
    "version",
    "meta.tag",
    "tool.release",
    "missing",
    "meta.missing",
    "version.version",
)

#: The seven cells of the VERSION_DOCUMENTS x KNOWN_FIELD_PATHS matrix that
#: resolve to a version, and the string every reader must produce for each.
#: Observed from live reads, not predicted: `wrong.toml` declares an integer
#: and `broken.json` is not JSON, so neither ever appears here, and
#: `version.version` walks into a scalar in every document that has one.
#: `test_the_resolving_table_is_exhaustive_over_the_document_matrix` asserts
#: the other thirty-five cells are `None`, which makes this a claim about the
#: whole matrix rather than a list of convenient examples.
RESOLVING_FIELDS = {
    ("info.yml", "version"): "6.0.0",
    ("meta.yaml", "meta.tag"): "v5",
    ("meta.yaml", "version"): "4.0.0",
    ("package.json", "meta.tag"): "v9",
    ("package.json", "version"): "1.2.3",
    ("pyproject.toml", "tool.release"): "3.0.0",
    ("pyproject.toml", "version"): "2.0.0",
}

#: Characters an arbitrary drawn field path is built from: every letter that
#: appears in a name the documents above actually carry, plus the separators
#: and digits. The set is spelled from those names on purpose. An alphabet
#: missing so much as the "s" of "version" cannot spell a resolving name at
#: any segment length, which would leave the arbitrary branch structurally
#: incapable of producing anything but a miss.
FIELD_ALPHABET = "versionmtagl._-0123456789"


def field_paths() -> st.SearchStrategy:
    """A field path: one of the known ones, or an arbitrary dotted name."""
    return st.one_of(
        st.sampled_from(KNOWN_FIELD_PATHS),
        st.lists(
            st.text(alphabet=FIELD_ALPHABET, min_size=1, max_size=9),
            min_size=1,
            max_size=3,
        ).map(".".join),
    )


def documents() -> st.SearchStrategy:
    """Bytes a version file may hold: arbitrary, or a plausible manifest."""
    return st.one_of(
        st.binary(max_size=400),
        st.text(max_size=200).map(lambda text: text.encode("utf-8")),
        st.builds(
            lambda value: (json.dumps({"version": value}) + "\n").encode("utf-8"),
            st.text(max_size=40),
        ),
    )


class VersionReaderAgreementTests(unittest.TestCase):
    """Every reader must answer one declaration the same way, or be pinned."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = Scenario()
        for index, root in enumerate(CANONICAL_ROOTS):
            name = f"c{index}"
            cls.scene.component(
                name,
                path=root,
                provider="leaf",
                version_source={"file": "package.json", "field": "version"},
            )
            for filename, content in VERSION_DOCUMENTS.items():
                prefix = "" if root == "." else f"{root}/"
                cls.scene.file(f"{prefix}{filename}", content)
        cls.scene.commit()
        cls.accessors = {
            mode: _SourceAccessor(cls.scene.root, mode) for mode in SOURCE_MODES
        }

    @classmethod
    def tearDownClass(cls) -> None:
        for accessor in cls.accessors.values():
            accessor.close()
        cls.scene.close()

    # -- premise: the harness can see a disagreement ------------------------

    def test_the_differential_harness_reports_a_disagreement_when_one_exists(self):
        """Premise for every agreement assertion below.

        A reader that answers differently must show up as more than one group
        in the partition. Without this the agreement property would pass just
        as happily if `_readings` collapsed every answer into one value.
        """
        source = {"file": "package.json", "field": "version"}
        honest = _readings(self.scene.root, "svc", source, self.accessors)
        self.assertEqual(len(partition(honest)), 1, describe(honest))

        class _Contrarian:
            latest_tag = staticmethod(lambda root, prefix: None)

            @staticmethod
            def version_read_file(repo_rel):
                return b'{"version": "0.0.0-different"}\n'

        rigged = _readings(
            self.scene.root,
            "svc",
            source,
            {**self.accessors, "contrarian": _Contrarian()},
        )
        self.assertEqual(len(partition(rigged)), 2, describe(rigged))
        self.assertEqual(rigged["contrarian"], "0.0.0-different")

    # -- the value axis, pinned exhaustively rather than sampled ------------

    def test_the_resolving_table_is_exhaustive_over_the_document_matrix(self):
        """Every cell of the matrix, so RESOLVING_FIELDS is a claim.

        Read the whole VERSION_DOCUMENTS x KNOWN_FIELD_PATHS product and
        require each cell to be the table's value or `None`. A field that
        started resolving, or stopped, would fail here rather than quietly
        changing what the property below is sampling.
        """
        for filename in sorted(VERSION_DOCUMENTS):
            for field in KNOWN_FIELD_PATHS:
                with self.subTest(file=filename, field=field):
                    results = _readings(
                        self.scene.root,
                        "svc",
                        {"file": filename, "field": field},
                        self.accessors,
                    )
                    self.assertEqual(len(partition(results)), 1, describe(results))
                    self.assertEqual(
                        results["disk"], RESOLVING_FIELDS.get((filename, field))
                    )

    def test_every_reader_returns_the_documented_version_on_every_root(self):
        """The value axis crossed with the component-root axis.

        Both branches special-case the root component, by different code, and
        both build a repository-relative path for a nested one. This is the
        assertion that they build the same path *and* read the same value from
        it, rather than agreeing on `None` because neither found anything.
        """
        for root in CANONICAL_ROOTS:
            for (filename, field), expected in sorted(RESOLVING_FIELDS.items()):
                with self.subTest(root=root, file=filename, field=field):
                    results = _readings(
                        self.scene.root,
                        root,
                        {"file": filename, "field": field},
                        self.accessors,
                    )
                    self.assertEqual(len(partition(results)), 1, describe(results))
                    for name in results:
                        self.assertEqual(results[name], expected, name)

    # -- the property -------------------------------------------------------

    @PROFILE
    @given(
        root=st.sampled_from(CANONICAL_ROOTS),
        filename=st.sampled_from(sorted(VERSION_DOCUMENTS)),
        field=field_paths(),
    )
    def test_every_reader_agrees_on_a_tracked_committed_version_source(
        self, root, filename, field
    ):
        """The miss space, which is most of the space and worth its own property.

        Thirty-five of the forty-two cells in the matrix resolve to nothing,
        and an arbitrary dotted name resolves to nothing more often still, so
        the great majority of examples here assert that four readers refuse in
        the same way — which is precisely the asymmetry the register claims
        exists between the disk fallback and the accessors. The two tables
        above carry the "and they agree on a value" half of the contract, so
        neither half depends on how often Hypothesis draws a resolving name.
        """
        source = {"file": filename, "field": field}
        results = _readings(self.scene.root, root, source, self.accessors)
        self.assertEqual(len(partition(results)), 1, describe(results))

    @IO_PROFILE
    @given(payload=documents())
    def test_the_disk_and_working_tree_readers_agree_on_arbitrary_content(
        self, payload
    ):
        """The content axis, widened past the committed matrix.

        The working-tree accessor reads the same bytes from the same path the
        disk fallback opens, so rewriting a tracked file needs no Git call and
        the whole byte space is reachable at the cost of one write per example.
        """
        target = self.scene.root / "svc" / "package.json"
        target.write_bytes(payload)
        try:
            source = {"file": "package.json", "field": "version"}
            results = _readings(
                self.scene.root,
                "svc",
                source,
                {"working-tree": self.accessors["working-tree"]},
            )
            self.assertEqual(len(partition(results)), 1, describe(results))
        finally:
            target.write_bytes(VERSION_DOCUMENTS["package.json"].encode("utf-8"))

    # -- the reader whose failure surface no test named ---------------------

    def test_the_disk_reader_reports_none_for_every_failure_it_owns(self):
        """Four instances, but only two `except` arms; see the test below.

        `ConfigError` and `GuardrailError` both subclass `ValueError`, so the
        last three rows are all caught by the `ValueError` arm and none of
        them can see a change to the third name in the clause. They are still
        worth running: each is a type `_read_bounded_path_bytes` actually
        raises, and the point of the row is that the caller gets `None` rather
        than the exception. What the clause *says* is asserted separately.
        """
        swallowed = (
            OSError("permission denied"),
            ValueError("File changed while hashing: svc/package.json"),
            GuardrailError("Hash guardrail exceeded: file too large"),
            ConfigError("Version source is not tracked"),
        )
        for failure in swallowed:
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(
                    versions, "_read_bounded_path_bytes", side_effect=failure
                ):
                    self.assertIsNone(
                        _read_version_file_bytes(Path("unused"), "svc/package.json")
                    )

    def test_the_disk_readers_except_clause_names_the_types_it_means_to_catch(self):
        """The clause itself, because the runtime table above cannot see it.

        Deleting `GuardrailError` from `except (OSError, ValueError,
        GuardrailError)` changes no behaviour any of those four rows can
        observe, because the `ValueError` arm already catches it. That makes
        the tuple a statement of intent that only the source can carry, so it
        is read here. A narrowing to `(OSError, ValueError)` is then a
        deliberate edit somebody has to argue for, not a silent one.
        """
        self.assertTrue(issubclass(GuardrailError, ValueError))
        self.assertTrue(issubclass(ConfigError, ValueError))
        tree = ast.parse(
            textwrap.dedent(inspect.getsource(_read_version_file_bytes))
        )
        handlers = [
            node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)
        ]
        self.assertEqual(len(handlers), 1)
        caught = handlers[0].type
        self.assertIsInstance(caught, ast.Tuple)
        self.assertEqual(
            [element.id for element in caught.elts],
            ["OSError", "ValueError", "GuardrailError"],
        )

    def test_the_disk_reader_propagates_a_failure_it_does_not_own(self):
        """Premise for the table above: the except clause is selective.

        A bare `except Exception: return None` would satisfy every assertion in
        that table while swallowing a programming error, so something has to
        prove the clause is narrow. `KeyboardInterrupt` is included because a
        `BaseException` that a read helper let escape must still reach the
        operator rather than being reported as a missing version.
        """
        for failure in (RuntimeError("bug"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(
                    versions, "_read_bounded_path_bytes", side_effect=failure
                ):
                    with self.assertRaises(type(failure)):
                        _read_version_file_bytes(
                            Path("unused"), "svc/package.json"
                        )

    # -- the divergences ----------------------------------------------------

    def test_the_two_branches_agree_on_every_component_path_spelling(self):
        """Both readers validate the component path before composing it.

        Canonical roots resolve normally; invalid spellings fail before either
        the disk path or an injected reader can interpret them.
        """
        source = {"file": "package.json", "field": "version"}
        for spelling in sorted(PATH_SPELLING_ANSWERS):
            with self.subTest(spelling=spelling):
                results = _readings(
                    self.scene.root, spelling, source, self.accessors
                )
                self.assertEqual(len(partition(results)), 1, describe(results))

    def test_each_component_path_spelling_keeps_the_answers_it_has_today(self):
        source = {"file": "package.json", "field": "version"}
        for spelling, expected in sorted(PATH_SPELLING_ANSWERS.items()):
            with self.subTest(spelling=spelling):
                results = _readings(
                    self.scene.root, spelling, source, self.accessors
                )
                self.assertEqual(results["disk"], expected[0])
                self.assertEqual(results["head"], expected[1])


#: The observed answer of (disk branch, head accessor) for one spelling of the
#: component path "svc". Canonical forms resolve and invalid spellings are
#: refused consistently by both reader branches.
PATH_SPELLING_ANSWERS = {
    "svc": ("1.2.3", "1.2.3"),
    "svc/": ("1.2.3", "1.2.3"),
    "svc//": (None, None),
    " svc ": (None, None),
    "/svc": (None, None),
    "./svc": (None, None),
}

#: What every reader answers when `svc/package.json` is declared but exists
#: nowhere at all: not on disk, not in the index, not in HEAD. Observed. The
#: three Git readers each fail at a different gate, and the disk fallback
#: reports `None` because `full_path.exists()` is False. Two tests below read
#: against this table rather than restating it, because what makes each of
#: them interesting is exactly which row moves off it.
ABSENT_VERSION_SOURCE_ANSWERS = {
    "disk": None,
    "head": (
        "RAISED ValueError: Path is absent from captured head tree: "
        "svc/package.json"
    ),
    "index": (
        "RAISED ValueError: Path is absent from captured index tree: "
        "svc/package.json"
    ),
    "working-tree": (
        "RAISED ConfigError: Version source is not tracked in the captured "
        "index: svc/package.json"
    ),
}


class VersionReaderRefusalTests(unittest.TestCase):
    """Where the disk fallback accepts and the accessor refuses, and back."""

    def _scene(self) -> Scenario:
        scene = Scenario()
        scene.component(
            "svc",
            path="svc",
            provider="leaf",
            version_source={"file": "package.json", "field": "version"},
        )
        scene.file("svc/main.py", "x\n")
        return scene

    def _read_every_way(self, scene: Scenario) -> Dict[str, Any]:
        accessors = {mode: _SourceAccessor(scene.root, mode) for mode in SOURCE_MODES}
        try:
            return _readings(
                scene.root,
                "svc",
                {"file": "package.json", "field": "version"},
                accessors,
            )
        finally:
            for accessor in accessors.values():
                accessor.close()

    def test_a_tracked_version_source_is_read_by_every_reader(self):
        """Premise for the refusal tests below: the fixture itself is readable."""
        with self._scene() as scene:
            scene.file("svc/package.json", '{"version": "9.9.9"}\n')
            scene.commit()
            results = self._read_every_way(scene)
        self.assertEqual(len(partition(results)), 1, describe(results))
        self.assertEqual(results["disk"], "9.9.9")

    def test_an_untracked_version_source_is_read_on_disk_and_refused_elsewhere(self):
        """The Git readers cannot tell an untracked file from no file at all.

        Every accessor row here is byte for byte the row the absent-file test
        below produces, even though the file is sitting on disk with a
        readable version in it. Only the disk fallback can see the difference,
        and it reports the version rather than refusing.
        """
        with self._scene() as scene:
            scene.commit()
            scene.file("svc/package.json", '{"version": "9.9.9"}\n')
            self.assertTrue((scene.root / "svc" / "package.json").exists())
            results = self._read_every_way(scene)
        self.assertEqual(results["disk"], "9.9.9")
        for mode in SOURCE_MODES:
            with self.subTest(mode=mode):
                self.assertEqual(
                    results[mode], ABSENT_VERSION_SOURCE_ANSWERS[mode]
                )

    def test_an_absent_version_source_is_none_on_disk_and_raises_elsewhere(self):
        """The register's mid-read race, without needing a race.

        A file that is gone is indistinguishable, to either reader, from a file
        that vanished between the stat and the read: the disk branch swallows
        the `ValueError` `read_bounded_file` raises and reports `None`, and the
        accessor branch lets it escape because `extract_version` catches only
        `OSError`, `CalledProcessError` and `GuardrailError`.
        """
        with self._scene() as scene:
            scene.commit()
            self.assertFalse((scene.root / "svc" / "package.json").exists())
            results = self._read_every_way(scene)
        self.assertEqual(results, ABSENT_VERSION_SOURCE_ANSWERS)
        for mode in SOURCE_MODES:
            with self.subTest(mode=mode):
                self.assertTrue(results[mode].startswith("RAISED "), results[mode])

    def test_a_git_mode_symlink_entry_is_refused_by_the_head_and_index_readers(self):
        """The symlink gate, reached by two of the four readers on this host.

        `update-index --cacheinfo 120000` writes the entry Git would write for
        a symlink, so the head and index accessors take the same branch a real
        symlink would take and refuse with the message pinned below. The other
        two readers cannot reach that branch, because the entry puts nothing on
        disk: the disk fallback stops at `full_path.exists()` and reports the
        same `None` it reports for a file that was never declared, and the
        working-tree reader gets past its tracked-in-index gate — which is what
        the index entry buys — and then fails on the read itself.

        Both of those rows are asserted against
        `ABSENT_VERSION_SOURCE_ANSWERS` rather than in isolation, so it is
        visible in the test which reader reached the symlink gate and which
        only got as far as a missing file. The working-tree row moving off
        that table is the evidence that the mode-120000 entry was seen at all;
        without it the row would read "not tracked in the captured index".
        """
        with self._scene() as scene:
            scene.file("svc/target.txt", "real.json")
            scene.file("svc/real.json", '{"version": "4.5.6"}\n')
            scene.commit()
            oid = scene.git("hash-object", "-w", "svc/target.txt")
            scene.git(
                "update-index",
                "--add",
                "--cacheinfo",
                f"120000,{oid},svc/package.json",
            )
            scene.commit_index("symlink entry")
            self.assertEqual(
                scene.git("ls-files", "--stage", "svc/package.json").split()[0],
                "120000",
            )
            self.assertIn(
                "svc/package.json", scene.git("ls-files").splitlines()
            )
            target = scene.root / "svc" / "package.json"
            self.assertFalse(target.exists())
            self.assertFalse(target.is_symlink())
            results = self._read_every_way(scene)

        for mode in ("head", "index"):
            with self.subTest(mode=mode, gate="symlink"):
                self.assertEqual(
                    results[mode],
                    "RAISED ConfigError: Version source must not be a symlink: "
                    "svc/package.json",
                )
                self.assertNotEqual(
                    results[mode], ABSENT_VERSION_SOURCE_ANSWERS[mode]
                )

        # The disk fallback never saw a symlink: nothing is on disk to be one.
        self.assertIsNone(results["disk"])
        self.assertEqual(results["disk"], ABSENT_VERSION_SOURCE_ANSWERS["disk"])

        # The working-tree reader did see the index entry — it cleared the
        # tracked gate the absent-file case fails at — and then found no file.
        self.assertEqual(
            results["working-tree"],
            "RAISED ValueError: File disappeared while hashing: svc/package.json",
        )
        self.assertNotEqual(
            results["working-tree"],
            ABSENT_VERSION_SOURCE_ANSWERS["working-tree"],
        )


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-138: the ten-megabyte boundary, on all three real readers
# ---------------------------------------------------------------------------


def _sized_manifest(total: int) -> str:
    """A parseable JSON manifest of exactly *total* bytes."""
    head = '{"version": "1.2.3", "pad": "'
    tail = '"}'
    padding = total - len(head) - len(tail)
    if padding < 0:  # pragma: no cover - the caller's sizes are far larger
        raise ValueError("requested size cannot hold a manifest")
    return head + ("a" * padding) + tail


class VersionFileCeilingTests(unittest.TestCase):
    """Exactly at the limit extracts; one byte over is diagnosed, not truncated."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.scenes: Dict[int, Scenario] = {}
        for size in (MAX_VERSION_FILE_BYTES, MAX_VERSION_FILE_BYTES + 1):
            scene = Scenario()
            scene.component(
                "svc",
                path="svc",
                provider="leaf",
                version_source={"file": "package.json", "field": "version"},
            )
            scene.file("svc/package.json", _sized_manifest(size))
            scene.commit()
            cls.scenes[size] = scene

    @classmethod
    def tearDownClass(cls) -> None:
        for scene in cls.scenes.values():
            scene.close()

    def test_the_fixture_really_is_the_size_the_boundary_needs(self):
        """Premise: an off-by-one in the fixture would fake either result."""
        for size, scene in sorted(self.scenes.items()):
            with self.subTest(size=size):
                observed = (scene.root / "svc" / "package.json").stat().st_size
                self.assertEqual(observed, size)

    def test_the_version_ceiling_never_exceeds_the_ceilings_that_clamp_it(self):
        """`read_file_limited` silently clamps with `min(max_bytes, ...)`.

        Raising MAX_VERSION_FILE_BYTES above either of these would leave the
        accessor path on the old limit while the disk fallback moved, so the
        same repository would extract a version on disk and not at head.
        """
        self.assertEqual(MAX_VERSION_FILE_BYTES, 10 * 1024 * 1024)
        self.assertLessEqual(
            MAX_VERSION_FILE_BYTES, min(MAX_HASH_FILE_BYTES, MAX_GIT_BLOB_BYTES)
        )
        self.assertEqual(
            min(MAX_VERSION_FILE_BYTES, MAX_HASH_FILE_BYTES),
            MAX_VERSION_FILE_BYTES,
        )

    def test_a_version_source_of_exactly_the_limit_extracts_on_every_reader(self):
        scene = self.scenes[MAX_VERSION_FILE_BYTES]
        source = {"file": "package.json", "field": "version"}
        self.assertEqual(extract_version(scene.root, "svc", source), "1.2.3")
        for mode in SOURCE_MODES:
            with self.subTest(mode=mode), _SourceAccessor(scene.root, mode) as acc:
                self.assertEqual(
                    len(acc.version_read_file("svc/package.json")),
                    MAX_VERSION_FILE_BYTES,
                )
                self.assertEqual(
                    extract_version(
                        scene.root,
                        "svc",
                        source,
                        acc.latest_tag,
                        read_file_fn=acc.version_read_file,
                    ),
                    "1.2.3",
                )

    def test_one_byte_over_the_limit_is_diagnosed_rather_than_truncated(self):
        scene = self.scenes[MAX_VERSION_FILE_BYTES + 1]
        source = {"file": "package.json", "field": "version"}
        self.assertIsNone(extract_version(scene.root, "svc", source))
        for mode in SOURCE_MODES:
            with self.subTest(mode=mode), _SourceAccessor(scene.root, mode) as acc:
                with self.assertRaises(GuardrailError) as raised:
                    acc.version_read_file("svc/package.json")
                message = str(raised.exception)
                self.assertTrue(
                    message.startswith("Hash guardrail exceeded: "), message
                )
                self.assertIn(f"({MAX_VERSION_FILE_BYTES + 1} bytes)", message)
                self.assertIsNone(
                    extract_version(
                        scene.root,
                        "svc",
                        source,
                        acc.latest_tag,
                        read_file_fn=acc.version_read_file,
                    )
                )


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-139: what validate-config checks, and what only generate finds
# ---------------------------------------------------------------------------

#: The exact stderr `generate` emits for every deferred version-source failure,
#: stripped of its trailing newline. The literal backslash-n after the colon is
#: not a typo: the human output layer escapes the control characters inside a
#: diagnostic, so the embedded newline of "Lockfile generation failed:\n"
#: survives to the terminal as two characters.
def _generate_failure(filename: str, field: str) -> str:
    return (
        "ERROR: Lockfile generation failed:\\nsvc: Configured version source did "
        f"not produce a version (file {filename!r}, field {field!r})\n"
        "Review the reported provider, source, or facet error. Use "
        "--allow-partial only when null slice facet inputs are intentional."
    )


GENERATE_FAILURE = _generate_failure("package.json", "version")

#: Every version-source failure the validation block cannot see, because it
#: reads only metadata and never opens the file. Each entry is the declared
#: filename, the payload, and the declared field. A payload given as an `int`
#: is a size rather than content: `_scene` builds a parseable manifest of
#: exactly that many bytes when the subtest runs, so the ten-megabyte case
#: costs nothing at import time and stays resident for one subtest instead of
#: the whole pytest session.
DEFERRED_FAILURES = {
    "unparseable-json": ("package.json", b"{not json at all\n", "version"),
    "field-does-not-resolve": ("package.json", b'{"name": "x"}\n', "version"),
    "field-stops-short": ("package.json", b'{"a": {"b": 1}}\n', "a.c"),
    "toml-value-is-not-a-string": ("pyproject.toml", b"version = 3\n", "version"),
    "not-utf8": ("package.json", b'{"version": "1.\xff"}\n', "version"),
    "over-the-size-ceiling": (
        "package.json",
        MAX_VERSION_FILE_BYTES + 1,
        "version",
    ),
}


class VersionSourceValidationSplitTests(unittest.TestCase):
    """validate-config passes; generate is where the document is finally read."""

    def _scene(self, filename: str, payload: Any, field: str) -> Scenario:
        if isinstance(payload, int):
            payload = _sized_manifest(payload).encode("ascii")
        scene = Scenario()
        scene.component(
            "svc",
            path="svc",
            provider="leaf",
            version_source={"file": filename, "field": field},
        )
        scene.file("svc/main.py", "x\n")
        target = scene.root / "svc" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        scene.commit()
        return scene

    def test_the_oversized_payload_really_is_over_the_ceiling(self):
        """Premise: the lazily built row must still be the size it claims.

        `over-the-size-ceiling` is declared as a number rather than as bytes,
        so nothing else in this class would notice if `_scene` stopped
        expanding it and wrote the digits instead.
        """
        filename, payload, field = DEFERRED_FAILURES["over-the-size-ceiling"]
        self.assertIsInstance(payload, int)
        self.assertGreater(payload, MAX_VERSION_FILE_BYTES)
        with self._scene(filename, payload, field) as scene:
            observed = (scene.root / "svc" / filename).stat().st_size
        self.assertEqual(observed, MAX_VERSION_FILE_BYTES + 1)

    def test_validate_config_rejects_the_metadata_it_does_check(self):
        """Premise: `validate-config` exiting 0 below is a decision, not a gap.

        A validation pass that reported nothing at all about version sources
        would satisfy every "exits 0" assertion in this class, so something has
        to show the block runs and can fail.
        """
        with self._scene("main.py", b"x\n", "version") as scene:
            scene.config["components"]["svc"]["version_source"] = {
                "file": "package.json",
                "field": "version",
            }
            scene.commit()
            result = run_cli(scene.root, "validate-config")
        self.assertEqual(result.returncode, 2)
        self.assertIn("CONFIG INVALID (1 issues):", result.stdout)
        self.assertIn(
            "Component 'svc' version_source.file not found: 'package.json' "
            "(looked for svc/package.json)",
            result.stdout,
        )

    def test_a_readable_version_source_satisfies_validation_and_generation(self):
        """Premise: the generate failures below are the document, not the fixture."""
        with self._scene("meta.yaml", b"version: 1.2.3\n", "version") as scene:
            validation = run_cli(scene.root, "validate-config")
            generation = run_cli(
                scene.root, "generate", "--source", "head", "--out", "out.lock.json"
            )
            lock = json.loads((scene.root / "out.lock.json").read_text("utf-8"))
        self.assertEqual(validation.returncode, 0)
        self.assertEqual(generation.returncode, 0)
        self.assertEqual(lock["components"]["svc"]["version"], "1.2.3")

    def test_every_deferred_failure_passes_validation_and_fails_generation(self):
        for label, (filename, payload, field) in sorted(DEFERRED_FAILURES.items()):
            with self.subTest(failure=label):
                with self._scene(filename, payload, field) as scene:
                    validation = run_cli(scene.root, "validate-config")
                    generation = run_cli(
                        scene.root,
                        "generate",
                        "--source",
                        "head",
                        "--out",
                        "out.lock.json",
                    )
                    self.assertEqual(validation.returncode, 0, validation.stdout)
                    self.assertEqual(validation.stdout.strip(), "Config is valid.")
                    self.assertEqual(generation.returncode, 2, generation.stderr)
                    self.assertEqual(
                        generation.stderr.strip(),
                        _generate_failure(filename, field),
                    )
                    self.assertFalse((scene.root / "out.lock.json").exists())

    def test_a_missing_optional_parser_is_also_only_found_at_generate(self):
        """PyYAML is an optional extra, so its absence is a deferred failure too."""
        with self._scene("meta.yaml", b"version: 1.2.3\n", "version") as scene:
            with mock.patch.object(versions, "yaml", None):
                validation = run_cli_in_process(scene.root, "validate-config")
                generation = run_cli_in_process(
                    scene.root, "generate", "--source", "head", "--out", "out.lock.json"
                )
        self.assertEqual(validation.returncode, 0, validation.stdout)
        self.assertEqual(validation.stdout.strip(), "Config is valid.")
        self.assertEqual(generation.returncode, 2, generation.stderr)
        self.assertEqual(
            generation.stderr.strip(),
            _generate_failure("meta.yaml", "version"),
        )

    def test_the_generate_stderr_is_exactly_what_a_shell_receives(self):
        with self._scene("package.json", b"{not json\n", "version") as scene:
            generation = run_cli(
                scene.root, "generate", "--source", "head", "--out", "out.lock.json"
            )
        self.assertEqual(generation.stdout, "")
        self.assertEqual(generation.stderr, GENERATE_FAILURE + "\n")

    def test_the_generate_diagnostic_names_the_component_the_file_and_the_field(self):
        """The deferred diagnostic identifies the declaration that failed."""
        with self._scene("package.json", b"{not json\n", "version") as scene:
            generation = run_cli(
                scene.root, "generate", "--source", "head", "--out", "out.lock.json"
            )
        self.assertIn("package.json", generation.stderr)
        self.assertIn("field 'version'", generation.stderr)


# ---------------------------------------------------------------------------
# OBL-GLOBS-022: the analyser's current column against the production selector
# ---------------------------------------------------------------------------

#: The component tree every selector below is evaluated against. It carries a
#: regular file named like a directory ("docs"), a directory with the same
#: stem plus a suffix ("docs2"), three nested levels, and three names that a
#: single "?" can tell apart.
ANALYSED_FILES = (
    "svc/api/v1.yaml",
    "svc/api/v2.yaml",
    "svc/api/nested/deep.yaml",
    "svc/api/nested/deeper/deepest.yaml",
    "svc/docs",
    "svc/docs2/readme.md",
    "svc/a.txt",
    "svc/ab.txt",
    "svc/abc.txt",
    "svc/pkg/__init__.py",
    "svc/pkg/mod.py",
)

#: Literal selectors, drawn as whole paths rather than composed from segments.
#: This is where the differential has two independent implementations to
#: compare — the analyser's `_literal_matches` against the
#: `is_dir_like or is_selected_directory` branch of `_expand_component_paths` —
#: so the pool is weighted onto paths that exist. Composing a literal from
#: three sampled segments produces a path that exists about one time in twenty,
#: and a differential drawn from a pool of misses compares empty against empty.
LITERAL_SELECTOR_BASES = (
    "api/v1.yaml",
    "api/v2.yaml",
    "api/nested/deep.yaml",
    "api/nested/deeper/deepest.yaml",
    "docs",
    "docs2/readme.md",
    "a.txt",
    "ab.txt",
    "pkg/__init__.py",
    "pkg/mod.py",
    "api",
    "api/nested",
    "api/nested/deeper",
    "docs2",
    "pkg",
    "docs/readme.md",
    "a.txt/x",
    "doc",
    "docs3",
    "api/v3.yaml",
    "missing",
)

#: The six bases above the production selector finds nothing for. Two of them
#: are deliberate: "docs/readme.md" and "a.txt/x" descend through a regular
#: file, which is the case a directory-inference bug is most likely to get
#: wrong in one implementation and not the other.
DEAD_LITERAL_BASES = (
    "a.txt/x",
    "api/v3.yaml",
    "doc",
    "docs/readme.md",
    "docs3",
    "missing",
)

#: Wildcard constructs the current matcher recognises, plus the plain segments
#: a glob selector needs around them.
GLOB_SEGMENTS = (
    "*",
    "**",
    "?",
    "*.yaml",
    "*.txt",
    "*.py",
    "a?",
    "a*",
    "[ab]*",
    "[!a]*",
    "[a-c].txt",
    "?.txt",
    "*[.]txt",
    "a**",
    "***",
)

PLAIN_SEGMENTS = ("api", "nested", "deeper", "docs", "docs2", "pkg", "missing")

#: The selector kinds the register names as untested on the current column,
#: each with the observed (legacy, current) pair that makes it interesting.
NAMED_SELECTOR_KINDS = {
    "single-character-wildcard": "a?.txt",
    "directory-literal-that-is-a-regular-file": "docs",
    "trailing-slash-on-a-regular-file": "docs/",
    "directory-literal-with-descendants": "api",
    "trailing-slash-with-descendants": "api/",
    "double-star": "**",
    "character-class": "[a-c].txt",
}

#: One boundary provider per v0.10 comparability class, with the class each one
#: lands in. Drawing this alongside the selector is what makes the uncompared
#: half of the oracle run at all: a config that always says "openapi" produces
#: nothing but "compared" declarations.
ANALYSED_PROVIDERS = {
    "openapi": "compared",
    "json-file": "compared",
    "implicit": "compared",
    "json-canonical": "compared for a literal, legacy-rejected for a glob",
    "path-hash": "legacy-rejected",
    "leaf": "not-applicable",
    "vendor-specific": "provider-specific",
}

#: Selectors the current contract rejects outright, with the exact detail the
#: analyser reports. This is the one uncompared status where the production
#: selector really does choose nothing, because `_expand_component_paths`
#: catches the same `ValueError` from `_normalize_declared_path` and skips the
#: declaration.
CURRENT_REJECTED_SELECTORS = {
    " docs ": (
        "Current Boundver rejects this declaration (must not have leading or "
        "trailing whitespace); Boundver 0.10 trimmed and evaluated 'docs'"
    ),
    "./docs": (
        "Current Boundver rejects this declaration (must not contain '.' path "
        "segments); Boundver 0.10 trimmed and evaluated './docs'"
    ),
    "a//b": (
        "Current Boundver rejects this declaration (must not contain empty "
        "path segments); Boundver 0.10 trimmed and evaluated 'a//b'"
    ),
    "a/../docs": (
        "Current Boundver rejects this declaration (must not contain '..' "
        "path segments; the path escapes its declared root); Boundver 0.10 "
        "trimmed and evaluated 'a/../docs'"
    ),
}

#: The exact detail an uncompared boundary declaration carries, per provider.
UNCOMPARED_PROVIDER_ANSWERS = {
    "path-hash": (
        "docs",
        "legacy-rejected",
        "Boundver 0.10 did not register path-hash as a public boundary provider",
    ),
    "json-canonical": (
        "api/*.yaml",
        "legacy-rejected",
        "Boundver 0.10 rejected glob selectors for the json-canonical boundary "
        "provider",
    ),
    "leaf": (
        "docs",
        "not-applicable",
        "The leaf boundary provider ignored boundary.paths in Boundver 0.10",
    ),
    "vendor-specific": (
        "a.txt",
        "provider-specific",
        "Selector semantics for boundary provider 'vendor-specific' cannot be "
        "inferred from the Boundver 0.10 built-ins",
    ),
}


def literal_selectors() -> st.SearchStrategy:
    """A literal selector, optionally spelled with a trailing slash."""
    return st.builds(
        lambda base, slash: base + ("/" if slash else ""),
        st.sampled_from(LITERAL_SELECTOR_BASES),
        st.booleans(),
    )


def glob_selectors() -> st.SearchStrategy:
    """One to three segments, at least one of which carries a wildcard."""
    return st.builds(
        lambda parts, slash: "/".join(parts) + ("/" if slash else ""),
        st.lists(
            st.one_of(st.sampled_from(GLOB_SEGMENTS), st.sampled_from(PLAIN_SEGMENTS)),
            min_size=1,
            max_size=3,
        ).filter(lambda parts: any(part in GLOB_SEGMENTS for part in parts)),
        st.booleans(),
    )


class MigrationCurrentColumnTests(unittest.TestCase):
    """The analyser's "current" column must be the production selector's answer."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = Scenario()
        cls.scene.component(
            "svc", path="svc", provider="openapi", boundary=["api/*.yaml"]
        )
        for path in ANALYSED_FILES:
            cls.scene.file(path, path + "\n")
        cls.scene.commit()
        cls.snapshot = _capture_git_source_snapshot(cls.scene.root, "head")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    def _declaration(self, selector: str, provider: str = "openapi") -> dict:
        config = {
            "project": "p",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": provider, "paths": [selector]},
                }
            },
            "slices": {},
        }
        analysis = migration.analyze_selector_migration(
            config,
            self.scene.root,
            source="head",
            snapshot=self.snapshot,
            lock_path="old.lock.json",
            lock_schema="boundary-lock/v3",
            migration_action="regenerate",
            migration_reason="regeneration required",
        )
        self.assertEqual(len(analysis["declarations"]), 1)
        return analysis["declarations"][0]

    def _selected(self, selector: str) -> Set[str]:
        return _expand_component_paths(
            self.scene.root,
            "svc",
            [selector],
            source="head",
            snapshot=self.snapshot,
        )

    # -- premises -----------------------------------------------------------

    def test_both_sides_are_looking_at_the_same_eleven_files(self):
        """Premise: agreement on counts would be vacuous over an empty tree."""
        self.assertEqual(
            sorted(self._selected("**")),
            sorted(path[len("svc/") :] for path in ANALYSED_FILES),
        )
        self.assertEqual(self._declaration("**")["current_match_count"], 11)

    def test_most_literal_selectors_the_strategy_draws_do_select_files(self):
        """Premise: the literal differential must meet on non-empty selections.

        `_literal_matches` and the literal branch of `_expand_component_paths`
        are the one pair of genuinely independent implementations in this
        comparison, and two implementations that both find nothing agree for
        free. Fifteen of the twenty-one bases select at least one file; the
        six that do not are named, so a change to the fixture tree that
        quietly emptied the pool fails here rather than hollowing out the
        property below.
        """
        live = {base for base in LITERAL_SELECTOR_BASES if self._selected(base)}
        dead = set(LITERAL_SELECTOR_BASES) - live
        self.assertEqual(sorted(dead), sorted(DEAD_LITERAL_BASES))
        self.assertEqual(len(live), 15)

    def test_the_legacy_column_disagrees_with_the_production_selector(self):
        """Premise: the oracle detects a column that is not the current one.

        v0.10 let `*` cross `/`, so `api/*.yaml` reached two nested files the
        current segment-aware matcher does not. Running the legacy column
        through the same comparison the property makes shows the comparison
        would fail if the current column drifted that way. This premise is
        load-bearing for the glob property in particular: the analyser's
        current-column glob matcher shares `_compile_path_glob_with_spender`
        with the production matcher, so `_match_text_glob` is the only
        independent implementation a glob example puts on the table.
        """
        declaration = self._declaration("api/*.yaml")
        selected = self._selected("api/*.yaml")
        self.assertEqual(declaration["current_match_count"], len(selected))
        self.assertNotEqual(declaration["legacy_match_count"], len(selected))
        self.assertEqual(declaration["legacy_match_count"], 4)
        self.assertEqual(declaration["impact"], "narrowed")
        self.assertEqual(
            declaration["legacy_only_examples"],
            ["api/nested/deep.yaml", "api/nested/deeper/deepest.yaml"],
        )

    def test_an_uncompared_declaration_can_still_select_files_in_production(self):
        """Premise: this is why the uncompared oracle is not "selects nothing".

        "Uncompared" is a statement about whether v0.10 semantics can be
        reconstructed, not about whether the declaration does anything. A
        `path-hash` boundary was not a public provider in v0.10, so the
        analyser withholds every count for it — while the current selector
        goes right on selecting all eleven files. An oracle that required an
        uncompared row to select nothing would fire on correct behaviour.
        """
        declaration = self._declaration("**", "path-hash")
        self.assertEqual(declaration["analysis_status"], "legacy-rejected")
        self.assertIsNone(declaration["current_match_count"])
        self.assertEqual(len(self._selected("**")), 11)

    def test_every_provider_class_reaches_the_status_it_is_drawn_for(self):
        """Premise: drawing the provider must actually vary the status.

        Without this the provider axis could sample seven names that all
        classify as comparable and the uncompared branch would stay dead while
        looking exercised.
        """
        reached = {
            self._declaration(selector, provider)["analysis_status"]
            for provider, (selector, _, _) in UNCOMPARED_PROVIDER_ANSWERS.items()
        }
        self.assertEqual(
            sorted(reached),
            ["legacy-rejected", "not-applicable", "provider-specific"],
        )
        for provider, (selector, status, detail) in sorted(
            UNCOMPARED_PROVIDER_ANSWERS.items()
        ):
            with self.subTest(provider=provider):
                declaration = self._declaration(selector, provider)
                self.assertEqual(declaration["analysis_status"], status)
                self.assertEqual(declaration["detail"], detail)
        for provider in ("openapi", "json-file", "implicit"):
            with self.subTest(provider=provider):
                declaration = self._declaration("api/*.yaml", provider)
                self.assertEqual(declaration["analysis_status"], "compared")
                self.assertIsNone(declaration["detail"])

    # -- the oracle ---------------------------------------------------------

    def _assert_uncompared_row(
        self, declaration: dict, selector: str, selected: Set[str]
    ) -> None:
        """What an uncompared declaration must report, and what it must not.

        Every comparison field is withheld and the impact says so. The
        selection is deliberately not asserted here — see the premise above —
        except for `current-rejected`, where the production selector really
        does skip the declaration for the same reason the analyser refuses to
        compare it.
        """
        self.assertEqual(declaration["impact"], "not-comparable", selector)
        for field in (
            "legacy_match_count",
            "current_match_count",
            "legacy_only_count",
            "current_only_count",
        ):
            self.assertIsNone(declaration[field], f"selector {selector!r}: {field}")
        self.assertEqual(declaration["legacy_only_examples"], [], selector)
        self.assertEqual(declaration["current_only_examples"], [], selector)
        self.assertIsInstance(declaration["detail"], str)
        self.assertNotEqual(declaration["detail"], "", selector)
        self.assertIn(declaration["selector_kind"], ("glob", "literal"), selector)
        if declaration["analysis_status"] == "current-rejected":
            self.assertEqual(
                selected,
                set(),
                f"selector {selector!r} is rejected by the current contract but "
                "the production selector still chose files",
            )

    def _assert_columns_agree(self, selector: str, provider: str = "openapi") -> None:
        declaration = self._declaration(selector, provider)
        try:
            selected = self._selected(selector)
        except GuardrailError as exc:
            self.fail(
                f"selector {selector!r}: the analyser reported "
                f"{declaration['current_match_count']!r} while the production "
                f"selector failed closed: {exc}"
            )
        if declaration["analysis_status"] != "compared":
            self._assert_uncompared_row(declaration, selector, selected)
            return
        self.assertEqual(
            declaration["current_match_count"],
            len(selected),
            f"selector {selector!r}: analyser said "
            f"{declaration['current_match_count']}, production selector chose "
            f"{sorted(selected)}",
        )
        self.assertLessEqual(
            set(declaration["current_only_examples"]),
            selected,
            f"selector {selector!r}: current-only example outside the selection",
        )
        self.assertTrue(
            set(declaration["legacy_only_examples"]).isdisjoint(selected),
            f"selector {selector!r}: legacy-only example inside the selection",
        )
        self.assertEqual(
            declaration["legacy_match_count"],
            declaration["current_match_count"]
            - declaration["current_only_count"]
            + declaration["legacy_only_count"],
            f"selector {selector!r}: the two columns do not overlap consistently",
        )

    # -- the properties -----------------------------------------------------

    @PROFILE
    @given(
        selector=literal_selectors(),
        provider=st.sampled_from(sorted(ANALYSED_PROVIDERS)),
    )
    def test_a_literal_selector_column_is_what_the_production_selector_selects(
        self, selector, provider
    ):
        """Two independent implementations of directory inference, compared.

        The analyser answers a literal with `_literal_matches`, which infers
        "this names a directory" by scanning for a candidate under `prefix +
        "/"`. Production answers it in `_expand_component_paths` with
        `is_dir_like or is_selected_directory`. Neither calls the other.
        """
        self._assert_columns_agree(selector, provider)

    @PROFILE
    @given(
        selector=glob_selectors(),
        provider=st.sampled_from(sorted(ANALYSED_PROVIDERS)),
    )
    def test_a_glob_selector_column_is_what_the_production_selector_selects(
        self, selector, provider
    ):
        """The legacy column is the independent one here, and it is checked.

        `_match_path_glob` and `_PathGlobOperation.matches` both reduce to
        `_compile_path_glob_with_spender` and
        `_match_compiled_path_glob_with_spender`, differing only in how they
        charge work, so the current-versus-production half of a glob example
        is close to a tautology and is asserted mostly to catch a wrapper that
        stops delegating. What earns the property its place is the rest of the
        oracle: `_match_text_glob` is a separate matcher, and every example
        requires its legacy-only files to be absent from the production
        selection and the two counts to overlap consistently.
        """
        self._assert_columns_agree(selector, provider)

    def test_the_selector_kinds_the_register_names_as_untested(self):
        for kind, selector in sorted(NAMED_SELECTOR_KINDS.items()):
            for provider in sorted(ANALYSED_PROVIDERS):
                with self.subTest(kind=kind, provider=provider):
                    self._assert_columns_agree(selector, provider)

    def test_a_selector_the_current_contract_rejects_is_reported_not_compared(self):
        """The fifth status, and the one where "selects nothing" is true.

        `_normalize_declared_path` raises for each of these, so the analyser
        marks the declaration `current-rejected` and quotes what v0.10 would
        have evaluated instead, while `_expand_component_paths` catches the
        same `ValueError` and skips the selector. The exact detail is pinned
        because it is the only place an operator learns what v0.10 did with
        the string they wrote.
        """
        for selector, detail in sorted(CURRENT_REJECTED_SELECTORS.items()):
            with self.subTest(selector=selector):
                declaration = self._declaration(selector)
                self.assertEqual(declaration["analysis_status"], "current-rejected")
                self.assertEqual(declaration["detail"], detail)
                self.assertEqual(self._selected(selector), set())
                self._assert_columns_agree(selector)

    def test_a_trailing_slash_on_a_regular_file_broadens_rather_than_diverging(self):
        """The one named kind whose two columns really do differ.

        v0.10 passed the trailing slash to Git's literal pathspec, which
        selected descendants and never the identically named regular file;
        the current selector treats `is_dir_like` as an addition rather than a
        restriction, so `docs/` now selects `docs`. Both sides report that
        honestly, which is what the differential above is asserting.
        """
        declaration = self._declaration("docs/")
        self.assertEqual(declaration["legacy_match_count"], 0)
        self.assertEqual(declaration["current_match_count"], 1)
        self.assertEqual(declaration["impact"], "broadened")
        self.assertEqual(declaration["current_only_examples"], ["docs"])
        self.assertEqual(self._selected("docs/"), {"docs"})

    def test_a_literal_that_descends_through_a_regular_file_selects_nothing(self):
        """The case the two directory inferences are most likely to split on.

        "docs" is a regular file, so "docs/readme.md" names a path under a
        non-directory. `_literal_matches` finds no candidate starting with
        "docs/readme.md/" and no candidate equal to it; production's
        `is_selected_directory` scan comes to the same conclusion by different
        code. Both must answer nothing, and neither may raise.
        """
        for selector in ("docs/readme.md", "a.txt/x"):
            with self.subTest(selector=selector):
                declaration = self._declaration(selector)
                self.assertEqual(declaration["analysis_status"], "compared")
                self.assertEqual(declaration["legacy_match_count"], 0)
                self.assertEqual(declaration["current_match_count"], 0)
                self.assertEqual(declaration["impact"], "unchanged")
                self.assertEqual(self._selected(selector), set())


# ---------------------------------------------------------------------------
# OBL-GLOBS-024: work accounting, and failing closed at the CLI
# ---------------------------------------------------------------------------


def charge_sites() -> Dict[str, Set[str]]:
    """Every call in the analyser that receives the `spend_evaluations` closure.

    The closure itself cannot be patched, but each function it is handed to is
    a module-level name, so the call graph is read out of the source rather
    than listed here. A charge added to a new callee is instrumented without
    anyone touching this file; a charge passed positionally, or to something
    that is not a plain module-level function, makes the discovery fail loudly
    instead of quietly going uncounted.
    """
    tree = ast.parse(
        textwrap.dedent(inspect.getsource(migration.analyze_selector_migration))
    )
    sites: Dict[str, Set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for argument in node.args:
            if isinstance(argument, ast.Name) and argument.id == "spend_evaluations":
                raise AssertionError(
                    "spend_evaluations is passed positionally to "
                    f"{ast.dump(node.func)}; the instrument cannot intercept it"
                )
        for keyword in node.keywords:
            value = keyword.value
            if not isinstance(value, ast.Name) or value.id != "spend_evaluations":
                continue
            if not isinstance(node.func, ast.Name):
                raise AssertionError(
                    "spend_evaluations reaches a non-module-level callee: "
                    f"{ast.dump(node.func)}"
                )
            sites.setdefault(node.func.id, set()).add(keyword.arg)
    return sites


class MigrationWorkAccountingTests(unittest.TestCase):
    """Every unit charged is reported, and a tripped limit prints nothing."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = Scenario()
        cls.scene.component(
            "svc",
            path="svc",
            provider="openapi",
            boundary=["api/*.yaml", "docs"],
            behavior=["*.txt", "pkg/**"],
        )
        cls.scene.component(
            "web",
            path="web",
            provider="json-file",
            boundary=["src/*.ts"],
            behavior=["README.md"],
        )
        for path in ANALYSED_FILES:
            cls.scene.file(path, path + "\n")
        for path in ("web/src/index.ts", "web/src/util.ts", "web/README.md"):
            cls.scene.file(path, path + "\n")
        cls.scene.file(
            "old.lock.json",
            json.dumps(
                {
                    "schema": "boundary-lock/v3",
                    "project": "scenario",
                    "components": {},
                    "slices": {},
                },
                indent=2,
            )
            + "\n",
        )
        cls.scene.commit()
        cls.snapshot = _capture_git_source_snapshot(cls.scene.root, "head")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    def _explain(self, runner, output_format: str):
        return runner(
            self.scene.root,
            "migrate-lock",
            "--lock",
            "old.lock.json",
            "--explain",
            "--format",
            output_format,
            "--source",
            "head",
        )

    def _run_instrumented(self, *, drop_first: bool = False) -> Tuple[List[int], dict]:
        """Analyse once, recording every amount handed to `spend_evaluations`."""
        recorded: List[Tuple[str, int]] = []
        dropped = {"done": not drop_first}

        def wrap(name: str, keywords: Set[str]):
            original = getattr(migration, name)

            def wrapper(*args, **kwargs):
                for keyword in keywords:
                    consumer = kwargs.get(keyword)
                    if consumer is None:
                        continue

                    def spy(amount, _consumer=consumer, _name=name):
                        if not dropped["done"]:
                            dropped["done"] = True
                        else:
                            recorded.append((_name, amount))
                        return _consumer(amount)

                    kwargs[keyword] = spy
                return original(*args, **kwargs)

            return mock.patch.object(migration, name, wrapper)

        sites = charge_sites()
        self.assertTrue(sites, "no charge site was discovered in the analyser")
        patches = [wrap(name, keywords) for name, keywords in sorted(sites.items())]
        for patch in patches:
            patch.start()
        try:
            analysis = migration.analyze_selector_migration(
                self.scene.config,
                self.scene.root,
                source="head",
                snapshot=self.snapshot,
                lock_path="old.lock.json",
                lock_schema="boundary-lock/v3",
                migration_action="regenerate",
                migration_reason="regeneration required",
            )
        finally:
            for patch in reversed(patches):
                patch.stop()
        return recorded, analysis

    def test_the_instrument_observes_a_charge_from_every_discovered_site(self):
        """Premise: an uninstrumented site would make the sum agree by accident."""
        recorded, _ = self._run_instrumented()
        self.assertEqual(
            sorted({name for name, _ in recorded}), sorted(charge_sites())
        )

    def test_dropping_one_observed_charge_breaks_the_accounting_identity(self):
        """Premise: the identity below is not true of any instrument at all."""
        recorded, analysis = self._run_instrumented(drop_first=True)
        self.assertNotEqual(
            sum(amount for _, amount in recorded),
            analysis["summary"]["match_evaluations"],
        )

    def test_the_reported_total_is_the_sum_of_every_charge(self):
        recorded, analysis = self._run_instrumented()
        self.assertEqual(
            sum(amount for _, amount in recorded),
            analysis["summary"]["match_evaluations"],
        )

    def test_the_reported_total_stays_under_the_published_cap(self):
        _, analysis = self._run_instrumented()
        self.assertEqual(migration.MAX_SELECTOR_MATCH_EVALUATIONS, 5_000_000)
        self.assertLessEqual(
            analysis["summary"]["match_evaluations"],
            migration.MAX_SELECTOR_MATCH_EVALUATIONS,
        )
        schema = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "spec"
                / "cli-output.migrate-lock.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            schema["properties"]["summary"]["properties"]["match_evaluations"][
                "maximum"
            ],
            migration.MAX_SELECTOR_MATCH_EVALUATIONS,
        )

    def test_explain_prints_a_complete_analysis_when_no_limit_is_tripped(self):
        """Premise: an empty stdout below means refusal, not a broken invocation.

        The refusal test measures `run_cli_in_process`, because a ceiling
        expressed as a module constant cannot be lowered across a process
        boundary. An emptiness claim has to be premised on the runner that
        produces the emptiness, so both runners are exercised here: the
        subprocess a user would actually type, and the in-process one. They
        must also agree, which is the only thing standing between a future
        change of output channel and a silently vacuous `stdout == ""`.
        """
        for runner in (run_cli, run_cli_in_process):
            for output_format in ("json", "text"):
                with self.subTest(runner=runner.__name__, format=output_format):
                    result = self._explain(runner, output_format)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotEqual(result.stdout.strip(), "")
        payloads = [
            json.loads(self._explain(runner, "json").stdout)
            for runner in (run_cli, run_cli_in_process)
        ]
        self.assertEqual(payloads[0], payloads[1])
        self.assertEqual(payloads[0]["summary"]["declaration_count"], 6)

    def test_a_tripped_limit_exits_usage_with_no_partial_json_on_stdout(self):
        limits = {
            "match evaluations": (
                "MAX_SELECTOR_MATCH_EVALUATIONS",
                3,
                "Migration selector analysis exceeds the 3-step aggregate "
                "matching-work limit",
            ),
            "analysed declarations": (
                "MAX_ANALYZED_DECLARATIONS",
                1,
                "Migration selector analysis exceeds the 1-declaration limit",
            ),
        }
        for label, (constant, value, detail) in sorted(limits.items()):
            for output_format in ("json", "text"):
                with self.subTest(limit=label, format=output_format):
                    with mock.patch.object(migration, constant, value):
                        result = self._explain(run_cli_in_process, output_format)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(
                        result.stderr.strip(),
                        f"error: migration analysis failed: {detail}",
                    )


# ---------------------------------------------------------------------------
# OBL-GLOBS-051: declaration order, and a summary that matches the array
# ---------------------------------------------------------------------------

#: Component names drawn for a generated config. Deliberately not in sorted
#: order as a tuple, so `st.lists(unique=True)` produces insertion orders that
#: differ from the order the analyser must emit.
GENERATED_NAMES = ("gamma", "alpha", "delta", "beta")

#: Providers spanning every v0.10 comparability class, so a generated config
#: mixes compared and uncompared declarations in one array.
GENERATED_PROVIDERS = (
    "openapi",
    "json-file",
    "json-canonical",
    "path-hash",
    "leaf",
    "vendor-specific",
)

GENERATED_SELECTORS = (
    "api/*.yaml",
    "docs",
    "*.txt",
    "pkg/**",
    "src/*.ts",
    "README.md",
    "**",
)

#: boundary before behavior, which is the order `_prepare_component_declarations`
#: builds its declaration groups in.
FACET_RANK = {"boundary": 0, "behavior": 1}


def _order_key(declaration: dict) -> Tuple[str, int, str]:
    return (
        declaration["component"],
        FACET_RANK[declaration["facet"]],
        declaration["selector"],
    )


@st.composite
def generated_configs(draw) -> dict:
    names = draw(
        st.lists(st.sampled_from(GENERATED_NAMES), min_size=1, max_size=4, unique=True)
    )
    components: Dict[str, Any] = {}
    for name in names:
        entry: Dict[str, Any] = {
            "path": draw(st.sampled_from(("svc", "web", "missing"))),
            "boundary": {
                "provider": draw(st.sampled_from(GENERATED_PROVIDERS)),
                "paths": draw(
                    st.lists(st.sampled_from(GENERATED_SELECTORS), max_size=3)
                ),
            },
        }
        if draw(st.booleans()):
            entry["behavior"] = {
                "paths": draw(
                    st.lists(st.sampled_from(GENERATED_SELECTORS), max_size=3)
                )
            }
        components[name] = entry
    return {"project": "p", "components": components, "slices": {}}


class MigrationSummaryConsistencyTests(unittest.TestCase):
    """The array is ordered, and the summary is arithmetic over the array."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = Scenario()
        cls.scene.component("svc", path="svc", provider="openapi", boundary=["**"])
        for path in ANALYSED_FILES:
            cls.scene.file(path, path + "\n")
        for path in ("web/src/index.ts", "web/src/util.ts", "web/README.md"):
            cls.scene.file(path, path + "\n")
        cls.scene.commit()
        cls.snapshot = _capture_git_source_snapshot(cls.scene.root, "head")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    def _analyze(self, config: dict) -> dict:
        return migration.analyze_selector_migration(
            config,
            self.scene.root,
            source="head",
            snapshot=self.snapshot,
            lock_path="old.lock.json",
            lock_schema="boundary-lock/v3",
            migration_action="regenerate",
            migration_reason="regeneration required",
        )

    def _assert_consistent(self, analysis: dict) -> None:
        declarations = analysis["declarations"]
        summary = analysis["summary"]
        keys = [_order_key(entry) for entry in declarations]
        self.assertEqual(
            keys,
            sorted(keys),
            "declarations are not ordered by component, then boundary before "
            f"behavior, then selector: {keys}",
        )
        self.assertEqual(len(declarations), summary["declaration_count"])
        self.assertEqual(
            summary["compared_declaration_count"]
            + summary["uncompared_declaration_count"],
            summary["declaration_count"],
        )
        compared = [
            entry for entry in declarations if entry["analysis_status"] == "compared"
        ]
        self.assertEqual(len(compared), summary["compared_declaration_count"])
        self.assertEqual(
            len(declarations) - len(compared),
            summary["uncompared_declaration_count"],
        )
        self.assertEqual(
            sum(1 for entry in compared if entry["impact"] != "unchanged"),
            summary["changed_declaration_count"],
        )
        self.assertEqual(
            sum(entry["legacy_only_count"] for entry in compared),
            summary["legacy_only_match_count"],
        )
        self.assertEqual(
            sum(entry["current_only_count"] for entry in compared),
            summary["current_only_match_count"],
        )
        for entry in declarations:
            if entry["analysis_status"] != "compared":
                self.assertIsNone(entry["legacy_only_count"])
                self.assertIsNone(entry["current_only_count"])

    def test_the_ordering_oracle_rejects_a_shuffled_array(self):
        """Premise: `keys == sorted(keys)` is not vacuously true.

        The declaration list of the fixed config below has more than one member
        and is not already sorted after a rotation, so an ordering check that
        did nothing would still pass on it.
        """
        analysis = self._analyze(FIXED_ORDERING_CONFIG)
        declarations = analysis["declarations"]
        self.assertGreater(len(declarations), 1)
        keys = [_order_key(entry) for entry in declarations]
        self.assertEqual(keys, sorted(keys))
        rotated = keys[1:] + keys[:1]
        self.assertNotEqual(rotated, sorted(rotated))

    def test_a_fixed_config_emits_its_declarations_in_the_documented_order(self):
        analysis = self._analyze(FIXED_ORDERING_CONFIG)
        self.assertEqual(
            [
                (entry["component"], entry["facet"], entry["selector"])
                for entry in analysis["declarations"]
            ],
            [
                ("alpha", "boundary", "api/*.yaml"),
                ("alpha", "boundary", "docs"),
                ("alpha", "behavior", "*.txt"),
                ("alpha", "behavior", "pkg/**"),
                ("beta", "boundary", "README.md"),
                ("beta", "boundary", "src/*.ts"),
            ],
        )
        self._assert_consistent(analysis)

    @PROFILE
    @given(config=generated_configs())
    def test_every_generated_config_is_ordered_and_adds_up(self, config):
        self._assert_consistent(self._analyze(config))

    def test_a_generated_config_can_produce_both_compared_and_uncompared_rows(self):
        """Premise: the identities above are exercised on a mixed array."""
        analysis = self._analyze(
            {
                "project": "p",
                "components": {
                    "alpha": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api/*.yaml"]},
                    },
                    "beta": {
                        "path": "svc",
                        "boundary": {"provider": "leaf", "paths": ["docs"]},
                    },
                },
                "slices": {},
            }
        )
        summary = analysis["summary"]
        self.assertEqual(summary["compared_declaration_count"], 1)
        self.assertEqual(summary["uncompared_declaration_count"], 1)
        self.assertEqual(summary["changed_declaration_count"], 1)
        self.assertEqual(summary["legacy_only_match_count"], 2)
        self.assertEqual(summary["current_only_match_count"], 0)
        self._assert_consistent(analysis)


#: Two components whose declared order is the reverse of the emitted one, and
#: whose selectors are unsorted within each facet. Written out rather than
#: drawn so the expected array can be spelled in full.
FIXED_ORDERING_CONFIG: Dict[str, Any] = {
    "project": "p",
    "components": {
        "beta": {
            "path": "web",
            "boundary": {"provider": "openapi", "paths": ["src/*.ts", "README.md"]},
        },
        "alpha": {
            "path": "svc",
            "boundary": {"provider": "openapi", "paths": ["docs", "api/*.yaml"]},
            "behavior": {"paths": ["pkg/**", "*.txt"]},
        },
    },
    "slices": {},
}


if __name__ == "__main__":  # pragma: no cover - convenience only
    unittest.main()
