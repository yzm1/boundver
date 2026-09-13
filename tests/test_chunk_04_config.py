"""Six promises about edges the configuration and the repository can carry.

The obligations gathered here share a shape: each names a state a user can
actually reach - a typo in a `consumers` array, a sparse checkout, a bare
repository, a commit landing while a lockfile is being written, a filename with
a quote in it, a version file that is a symlink - and asserts that boundver
either handles it or refuses it, but never quietly produces an answer that is
wrong. What made them hard to test is that most of these states cannot be
reached through boundver's own front door. A dangling consumer edge is rejected
by validation before any closure sees it, so the defensive filter behind
validation had to be reached through the graph helpers directly. A sparse index
entry, a symlink blob and an undecodable pathname cannot be created on this host
with `open()` at all, so the fixtures build them by writing index records with
`git update-index --cacheinfo` and `git update-index -z --index-info`, which
reach the object store without the filesystem ever seeing the name. A ref
moving mid-operation had to be injected inside `_GitBlobSession.read_blob`,
because the window it opens is a few milliseconds wide and cannot be hit by
racing a real process against it.

Three of the six turned out to disagree with the code, and all three are pinned
rather than argued with. Source-mode parity fails for a sparse checkout: with
`skip-worktree` set and the file absent from disk - which is what a cone sparse
checkout leaves behind, and which `git status` calls clean - `head` and `index`
hash the captured index blob while `working-tree` drops the path from
`list_files` entirely and produces a different digest. `GitSourceSnapshot`'s own
docstring says an absent skip-worktree path is to be read as its captured index
identity, and `_working_tree_name_status` does exactly that, so the change
reporter and the hasher disagree with each other inside one release. The second
divergence is narrower and belongs to this platform: Git on Windows will store
an undecodable pathname in a tree but Python's `os.fsdecode` cannot spell it, so
`_parse_name_status_entries` aborts with a `UnicodeDecodeError` instead of
reproducing the path. That is a `ValueError` subclass, so the CLI still
diagnoses rather than tracebacks, and the test says so rather than leaving the
blast radius implied. The third is the failure OBL-GIT-SOURCE-136 predicts in
its own text: the CLI's only protection against the bare `ValueError` kinds the
version reader raises is a handful of hand-written `except ValueError` clauses,
and `review` has none, so a reader failing mid-operation escapes `main` and
reaches the user as a `ValueError` traceback with exit 1 while `generate`,
`verify` and `why` diagnose the same failure at exit 2 and `status` reports it
on stdout at exit 0.

Reaching that state took a poisoned reader, and the file argues for it rather
than helping itself to it. Config validation fail-closes every version-source
failure a real repository can hold at `head` or `index` before the reader is
ever called - a test drives all six commands against a real 120000 index entry
and observes that not one of them reaches `version_read_file` - so the only
kinds left are the mid-read races the obligation names, an editor saving a
manifest while `verify` runs, which cannot be produced on demand. So the reader
is replaced with one that raises the bare `ValueError` those races raise, inside
a real subprocess running the real `main`, and the premise that the replacement
fires is the run beside it with the reader left alone.

The repository state matrix needed more than twelve repositories. A worktree
carrying no lockfile makes `verify`, `status` and `review` refuse on the missing
file, and that refusal is identical in every state and says nothing about any of
them, so each state that can carry a committed lockfile is built with one and
with enough history for `review` to compare a real range. All sixty cells are
pinned by exit code and first diagnostic line rather than swept for tracebacks,
a separate test shows the verify column moving when the lockfile goes stale, and
the matrix component is a `path-hash` boundary so boundary resolution is
exercised inside a linked worktree and a shallow clone rather than skipped by a
`leaf` provider that resolves to nothing.

The register's own reasoning needed correcting twice as well, and the file says
where. `verify --status` is named as a command that reads a version source; the
parser registers no such flag, `status` is a separate subcommand, and `explain`
turns out to read no version source at all - the set of commands that call
`_SourceAccessor.version_read_file` is observed here rather than listed, and it
is generate, verify, status, why and review. And the claim that an oversized
version file "degrades one component" while a symlinked one aborts the
operation is only half true: `_generation_errors` promotes a `version_errors`
entry into an operation-wide `Lockfile generation failed` that `strict=False`
is documented not to relax, so the two spellings have the same blast radius
under `generate` and differ only under `verify`, which does report on the
healthy component alongside the broken one - and says so by asserting the whole
issue list, including the mismatches the healthy component contributes when it
drifts too. Every absence asserted below is preceded by a test proving the same
mechanism reports the thing when it is genuinely there, because an absence
observed on a code path that was never entered is the failure this suite keeps
producing.

Covers OBL-CONFIG-029, OBL-GIT-SOURCE-059, OBL-GIT-SOURCE-060,
OBL-GIT-SOURCE-061, OBL-GIT-SOURCE-118 and OBL-GIT-SOURCE-136.
"""

from __future__ import annotations

import ast
import builtins
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest import mock

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from boundver import _git as git
from boundver import core, versions
from boundver._cli_parser import build_parser
from boundver._config import validate_config
from boundver._consumer_graph import (
    affected_consumer_groups,
    affected_consumers,
    consumer_closure,
    resolve_slice_components,
)
from boundver._git import (
    _capture_git_source_snapshot,
    _parse_name_status_entries,
    changed_paths_since_ref,
)
from boundver._hashing import (
    _capture_working_tree_ancestors,
    _verify_working_tree_ancestors,
)
from boundver._lockfile import _SourceAccessor, generate_lockfile, verify_lockfile
from boundver._utils import BoundverError, ConfigError, GuardrailError
from boundver.versions import MAX_VERSION_FILE_BYTES, extract_version

from tests._parity import run_cli, run_cli_in_process
from tests._repo_fixtures import init_git_repo
from tests._scenarios import SOURCE_MODES, Scenario

#: The first line Python writes for any uncaught exception. Every state in the
#: repository matrix is checked against this rather than against exit codes
#: alone, because a handler that prints a diagnostic *and* lets a traceback
#: through would otherwise read as a clean refusal.
TRACEBACK_MARKER = "Traceback (most recent call last)"


def _run_git(root: Path, *args: str, **kwargs) -> subprocess.CompletedProcess:
    """Run one git command in *root*, returning the completed process."""
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, check=True, **kwargs
    )


def _hash_blob(root: Path, content: bytes) -> str:
    """Write *content* into the object store and return its blob id."""
    return _run_git(root, "hash-object", "-w", "--stdin", input=content).stdout.decode().strip()


#: How `verify_lockfile` abbreviates a digest in an issue line: twelve hex
#: characters and an ellipsis.
_ABBREVIATED_DIGEST = re.compile(r"[0-9a-f]{12}\.\.\.")


def _elide_digests(issue: str) -> str:
    """Replace abbreviated digests so a whole issue list can be compared.

    The list is asserted in full below, and every part of it that carries
    meaning - which component, which facet, which message - is compared
    literally. The digests themselves are content hashes of the fixture, so
    pinning them would make the test fail for changes to the hashing scheme
    that have nothing to do with the obligation it covers.
    """
    return _ABBREVIATED_DIGEST.sub("<digest>", issue)


# ---------------------------------------------------------------------------
# OBL-CONFIG-029: a consumer edge naming a component that does not exist
# ---------------------------------------------------------------------------


#: Every entry point that turns a component's `consumers` array into a reported
#: name list. Each is keyed by its spelling so a leak names the caller that
#: leaked, and each is driven from the same two-node graph so the answers are
#: directly comparable.
CONSUMER_GRAPH_QUERIES = {
    "consumer_closure": lambda components: consumer_closure(components, ["a"]),
    "consumer_closure(include_seeds)": lambda components: consumer_closure(
        components, ["a"], include_seeds=True
    ),
    "affected_consumers": lambda components: affected_consumers(components, "a"),
    "affected_consumers(transitive)": lambda components: affected_consumers(
        components, "a", transitive=True
    ),
    "affected_consumer_groups": lambda components: affected_consumer_groups(
        components, "a"
    )["components"],
    "affected_consumer_groups(transitive)": lambda components: (
        affected_consumer_groups(components, "a", transitive=True)["components"]
    ),
    "resolve_slice_components(closure_of)": lambda components: (
        resolve_slice_components({"closure_of": "a"}, components)
    ),
}

#: `a` declares two downstream edges; only one of them is a declared component.
GRAPH_WITH_DANGLING_EDGE = {"a": {"consumers": ["b", "ghost"]}, "b": {"consumers": []}}

#: The same declaration with `ghost` promoted to a real component. This is the
#: premise graph: it proves every query above would report `ghost` if the name
#: resolved, so the absence asserted against the graph above is a real filter
#: rather than a query that never looked.
GRAPH_WITH_EVERY_EDGE_DECLARED = {
    "a": {"consumers": ["b", "ghost"]},
    "b": {"consumers": []},
    "ghost": {"consumers": []},
}


def _consumer_scenario(*, declare_ghost: bool) -> Scenario:
    """Two (or three) components where `a` names `ghost` as a consumer."""
    scene = Scenario()
    names = ("a", "b", "ghost") if declare_ghost else ("a", "b")
    for name in names:
        scene.component(
            name,
            path=name,
            provider="path-hash",
            boundary=["*.json"],
            consumers=["b", "ghost"] if name == "a" else None,
        )
        scene.json_file(f"{name}/x.json", {"value": 1})
    scene.commit()
    return scene


class UnknownConsumerEdgeTests(unittest.TestCase):
    """OBL-CONFIG-029: validation rejects it, and no closure ever reports it."""

    def test_every_graph_query_drops_a_consumer_that_is_not_a_declared_component(self):
        for label, query in CONSUMER_GRAPH_QUERIES.items():
            with self.subTest(query=label):
                self.assertNotIn("ghost", query(GRAPH_WITH_DANGLING_EDGE))

    def test_every_graph_query_reports_that_consumer_once_it_is_declared(self):
        """The premise: each query does look at the edge it is being asked about."""
        for label, query in CONSUMER_GRAPH_QUERIES.items():
            with self.subTest(query=label):
                self.assertIn("ghost", query(GRAPH_WITH_EVERY_EDGE_DECLARED))

    def test_the_surviving_edge_is_reported_in_full(self):
        """Dropping the dangling name must not drop the good name beside it."""
        expected = {
            "consumer_closure": ["b"],
            "consumer_closure(include_seeds)": ["a", "b"],
            "affected_consumers": ["b"],
            "affected_consumers(transitive)": ["b"],
            "affected_consumer_groups": ["b"],
            "affected_consumer_groups(transitive)": ["b"],
            "resolve_slice_components(closure_of)": ["a", "b"],
        }
        self.assertEqual(sorted(expected), sorted(CONSUMER_GRAPH_QUERIES))
        for label, query in CONSUMER_GRAPH_QUERIES.items():
            with self.subTest(query=label):
                self.assertEqual(query(GRAPH_WITH_DANGLING_EDGE), expected[label])

    def test_an_external_consumer_terminal_is_not_filtered_against_components(self):
        """External terminals are labels, not component names, so they survive."""
        graph = {"a": {"consumers": ["ghost"], "external_consumers": ["team-x"]}}
        self.assertEqual(affected_consumers(graph, "a"), ["team-x"])

    def test_an_explicit_slice_member_list_keeps_an_undeclared_name(self):
        """Pin the boundary of the filter: only `consumers` edges are filtered.

        `resolve_slice_components` filters a `closure_of` slice through the
        component map but returns an explicit `components` list verbatim, which
        is why config validation carries the membership check for that spelling.
        """
        self.assertEqual(
            resolve_slice_components(
                {"components": ["a", "ghost"]}, GRAPH_WITH_DANGLING_EDGE
            ),
            ["a", "ghost"],
        )

    def test_validate_config_rejects_the_declaration_the_closure_ignores(self):
        with _consumer_scenario(declare_ghost=False) as scene:
            self.assertEqual(
                validate_config(scene.config, scene.root),
                ["Component 'a' references unknown consumer: ghost"],
            )

    def test_validate_config_accepts_the_same_declaration_once_ghost_exists(self):
        """The premise for the rejection above: nothing else in this config fails."""
        with _consumer_scenario(declare_ghost=True) as scene:
            self.assertEqual(validate_config(scene.config, scene.root), [])

    def test_no_affected_consumers_line_or_impact_row_names_the_dangling_edge(self):
        with _consumer_scenario(declare_ghost=False) as scene:
            locked = scene.generate()
            scene.json_file("a/x.json", {"value": 2})
            scene.commit("drift")
            impact: List[dict] = []
            issues = verify_lockfile(
                scene.config, locked, scene.root, source="head", consumer_impact=impact
            )
            affected = [line for line in issues if line.startswith("AFFECTED CONSUMERS")]
            self.assertEqual(affected, ["AFFECTED CONSUMERS a: b"])
            self.assertEqual(
                impact,
                [
                    {
                        "component": "a",
                        "facets": ["boundary"],
                        "components": ["b"],
                        "external_consumers": [],
                        "transitive": False,
                    }
                ],
            )

    def test_the_same_drift_names_ghost_once_ghost_is_a_component(self):
        """The premise: this drift really does reach the impact reporter."""
        with _consumer_scenario(declare_ghost=True) as scene:
            locked = scene.generate()
            scene.json_file("a/x.json", {"value": 2})
            scene.commit("drift")
            impact: List[dict] = []
            issues = verify_lockfile(
                scene.config, locked, scene.root, source="head", consumer_impact=impact
            )
            affected = [line for line in issues if line.startswith("AFFECTED CONSUMERS")]
            self.assertEqual(affected, ["AFFECTED CONSUMERS a: b, ghost"])
            self.assertEqual(impact[0]["components"], ["b", "ghost"])


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-059: source-mode parity across real index states
# ---------------------------------------------------------------------------


#: Content shapes the hashing layer treats specially: CRLF is folded only when
#: the bytes carry no NUL, a lone CR is not a line ending, and a NUL makes the
#: whole file binary. The parity property draws from these so an example is
#: dense in the cases where the three source modes could plausibly diverge.
CONTENT_SHAPES = (
    b"plain\n",
    b"crlf\r\nsecond\r\n",
    b"lone\rcr\r",
    b"nul\x00byte\r\n",
    b"",
    b"trailing-no-newline",
    b"\xc3\xa9 utf8 \xe4\xb8\xad\n",
)

PARITY_PROFILE = settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


def _parity_scenario() -> Scenario:
    """One component whose boundary selects every YAML file under `api/`."""
    scene = Scenario()
    scene.component("svc", path="svc", provider="path-hash", boundary=["api/*.yaml"])
    return scene


class SourceModeParityUnderIndexStatesTests(unittest.TestCase):
    """OBL-GIT-SOURCE-059: head, index and working-tree on a clean tree."""

    def _clean_scene(self) -> Scenario:
        scene = _parity_scenario()
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.file("svc/api/v2.yaml", "openapi: 3.1.0\nx: 2\n")
        scene.commit()
        return scene

    def _digests(self, scene: Scenario) -> Dict[str, str]:
        return {
            mode: scene.generate(source=mode)["components"]["svc"]["fingerprints"][
                "boundary"
            ]
            for mode in SOURCE_MODES
        }

    @given(
        bodies=st.lists(
            st.sampled_from(CONTENT_SHAPES), min_size=1, max_size=4
        )
    )
    @example(bodies=[b"crlf\r\n", b"nul\x00byte\r\n"])
    @PARITY_PROFILE
    def test_a_clean_tree_hashes_the_same_under_every_source_mode(self, bodies):
        with _parity_scenario() as scene:
            for index, body in enumerate(bodies):
                target = scene.root / "svc" / "api" / f"f{index}.yaml"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
            scene.commit()
            self.assertEqual(scene.git("status", "--porcelain"), "")
            digests = self._digests(scene)
            self.assertEqual(
                len(set(digests.values())), 1, f"source modes disagreed: {digests}"
            )

    def test_a_content_change_moves_the_digest_the_property_compares(self):
        """The premise: agreement above is agreement, not a constant."""
        with self._clean_scene() as scene:
            before = self._digests(scene)
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\nchanged: true\n")
            scene.commit("edit")
            after = self._digests(scene)
            for mode in SOURCE_MODES:
                with self.subTest(source=mode):
                    self.assertNotEqual(before[mode], after[mode])

    # -- real skip-worktree entries -----------------------------------------

    def _sparse_scene(self, *, remove_from_disk: bool) -> Scenario:
        scene = self._clean_scene()
        scene.git("update-index", "--skip-worktree", "svc/api/v2.yaml")
        if remove_from_disk:
            (scene.root / "svc" / "api" / "v2.yaml").unlink()
        return scene

    def test_a_skip_worktree_entry_is_really_tagged_S_in_the_index(self):
        """The premise for every sparse assertion below: the fixture is real."""
        with self._sparse_scene(remove_from_disk=True) as scene:
            tagged = dict(
                (line[2:], line[:1])
                for line in scene.git("ls-files", "--cached", "-t").splitlines()
            )
            self.assertEqual(tagged["svc/api/v2.yaml"], "S")
            self.assertEqual(tagged["svc/api/v1.yaml"], "H")
            snapshot = _capture_git_source_snapshot(scene.root, "index")
            self.assertEqual(
                sorted(snapshot.skip_worktree_paths), ["svc/api/v2.yaml"]
            )

    def test_a_skip_worktree_entry_still_on_disk_agrees_across_every_source(self):
        with self._sparse_scene(remove_from_disk=False) as scene:
            self.assertEqual(scene.git("status", "--porcelain"), "")
            digests = self._digests(scene)
            self.assertEqual(
                len(set(digests.values())), 1, f"source modes disagreed: {digests}"
            )

    def test_a_sparse_absent_skip_worktree_path_hashes_the_same_everywhere(self):
        """An intentionally unmaterialized path uses its captured index blob."""
        with self._sparse_scene(remove_from_disk=True) as scene:
            self.assertEqual(scene.git("status", "--porcelain"), "")
            digests = self._digests(scene)
            self.assertEqual(len(set(digests.values())), 1, f"{digests}")

    def test_the_sparse_working_tree_listing_and_reader_use_the_index_entry(self):
        """Listing and content access agree on the captured sparse identity."""
        with self._sparse_scene(remove_from_disk=True) as scene:
            self.assertEqual(scene.git("status", "--porcelain"), "")
            self.assertEqual(scene.git("diff", "HEAD", "--name-status"), "")
            digests = self._digests(scene)
            self.assertEqual(digests["head"], digests["index"])
            self.assertEqual(digests["working-tree"], digests["head"])

            listings = {}
            for mode in SOURCE_MODES:
                accessor = _SourceAccessor(scene.root, mode)
                try:
                    listings[mode] = accessor.list_files("svc")
                finally:
                    accessor.close()
            self.assertEqual(
                listings["head"], ["svc/api/v1.yaml", "svc/api/v2.yaml"]
            )
            self.assertEqual(listings["index"], listings["head"])
            self.assertEqual(listings["working-tree"], listings["head"])

            accessor = _SourceAccessor(scene.root, "working-tree")
            try:
                content = accessor.read_file("svc/api/v2.yaml")
            finally:
                accessor.close()
            self.assertEqual(content, b"openapi: 3.1.0\nx: 2\n")
            self.assertEqual(content.git_mode, "100644")
            self.assertEqual(content.git_object_type, "blob")

    def test_the_change_reporter_does_treat_the_absent_sparse_path_as_present(self):
        """Why the divergence is one: the same release disagrees with itself.

        `_working_tree_name_status` reads an absent skip-worktree path as its
        captured index identity and reports no change, which is what
        `GitSourceSnapshot`'s docstring promises. The hasher above does not.
        """
        with self._sparse_scene(remove_from_disk=True) as scene:
            snapshot = _capture_git_source_snapshot(scene.root, "index")
            self.assertEqual(
                changed_paths_since_ref(
                    scene.root, scene.head(), source="working-tree", snapshot=snapshot
                ),
                [],
            )

    # -- real intent-to-add entries -----------------------------------------

    def _intent_to_add_scene(self, path: str) -> Scenario:
        scene = self._clean_scene()
        target = scene.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"openapi: 3.1.0\nx: 3\n")
        scene.git("add", "--intent-to-add", path)
        return scene

    def test_an_intent_to_add_path_outside_every_component_keeps_parity(self):
        """The premise: the fixture does not disturb an unrelated component."""
        with self._intent_to_add_scene("other/new.yaml") as scene:
            self.assertIn("other/new.yaml", scene.git("ls-files", "--cached"))
            digests = self._digests(scene)
            self.assertEqual(
                len(set(digests.values())), 1, f"source modes disagreed: {digests}"
            )

    def test_an_intent_to_add_path_inside_a_component_leaves_the_tree_unclean(self):
        """Pin the outcome: the parity precondition is not satisfiable here.

        An intent-to-add entry is by construction a path the index tracks and
        no tree contains, so `git status` reports it and the obligation's
        "clean tree with index matching HEAD" cannot hold. What must still hold
        is that head and index agree with each other and that the working tree
        differs only by the added path.
        """
        with self._intent_to_add_scene("svc/api/v3.yaml") as scene:
            # Read the raw record: `git` strips, and the leading blank of the
            # " A" porcelain code is the half that says "not staged".
            self.assertEqual(
                scene.git_bytes("status", "--porcelain", "-z"),
                b" A svc/api/v3.yaml\x00",
            )
            digests = self._digests(scene)
            self.assertEqual(digests["head"], digests["index"])
            self.assertNotEqual(digests["working-tree"], digests["head"])

            snapshot = _capture_git_source_snapshot(scene.root, "index")
            self.assertIn("svc/api/v3.yaml", snapshot.tracked_paths)
            self.assertNotIn("svc/api/v3.yaml", snapshot.entries)


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-060: every reachable repository state has a defined outcome
# ---------------------------------------------------------------------------


#: The component declaration every state in the matrix carries, so the states
#: differ only in the shape of the repository around it. The provider is
#: `path-hash` rather than `leaf`: `leaf` resolves to an empty entry list and a
#: null `fingerprints.boundary`, which would leave boundary resolution
#: unexercised in every state of the matrix.
_MATRIX_CONFIG = {
    "project": "matrix",
    "components": {
        "svc": {"path": "svc", "boundary": {"provider": "path-hash", "paths": ["*.py"]}}
    },
}

#: Every command the obligation names, with the smallest argv that reaches its
#: repository inspection. `review` needs endpoints or it refuses on argument
#: parsing before it ever opens the repository. Both `generate` spellings write
#: to a scratch name so that regenerating never disturbs the `boundary.lock.json`
#: the state builders commit, which is what lets the other three commands
#: inspect the state instead of refusing on a missing file.
MATRIX_COMMANDS = {
    "generate": ("generate", "--source", "head", "--out", "generated.lock.json"),
    "generate --source working-tree": (
        "generate",
        "--source",
        "working-tree",
        "--out",
        "generated.lock.json",
    ),
    "verify": ("verify",),
    "status": ("status",),
    "review": ("review", "--base", "HEAD", "--target", "HEAD"),
}

#: The states whose shape permits a committed lockfile at HEAD. The other seven
#: cannot have one: two have no commit, three are not repositories boundver will
#: open at all, and `submodule-gitlink` cannot produce a lockfile because
#: hashing its own component fails. Asserted as a premise below rather than
#: assumed, because it is exactly the difference between a `verify` row that
#: inspects the state and a `verify` row that reports a missing file.
STATES_WITH_A_COMMITTED_LOCKFILE = {
    "committed-history",
    "detached-head",
    "linked-worktree",
    "shallow-clone",
    "conflicted-merge",
}


def _write_matrix_config(root: Path) -> None:
    (root / "svc").mkdir(parents=True, exist_ok=True)
    (root / "svc" / "main.py").write_text("x\n", encoding="utf-8")
    (root / "boundary.config.json").write_text(
        json.dumps(_MATRIX_CONFIG, indent=2) + "\n", encoding="utf-8"
    )


class RepositoryStateMachineTests(unittest.TestCase):
    """OBL-GIT-SOURCE-060: twelve Git states, every command, no traceback."""

    @classmethod
    def setUpClass(cls):
        cls._scenes: List[Scenario] = []
        cls._temporary: List[tempfile.TemporaryDirectory] = []
        cls.states: Dict[str, Path] = {}
        for label, build in (
            ("committed-history", cls._state_committed),
            ("unborn-empty-index", cls._state_unborn_empty),
            ("unborn-staged-index", cls._state_unborn_staged),
            ("detached-head", cls._state_detached),
            ("bare-repository", cls._state_bare),
            ("linked-worktree", cls._state_linked_worktree),
            ("shallow-clone", cls._state_shallow),
            ("submodule-gitlink", cls._state_gitlink),
            ("refs-directory-removed", cls._state_no_refs),
            ("conflicted-merge", cls._state_conflicted),
            ("no-git-marker", cls._state_no_git),
            ("dangling-git-file", cls._state_dangling_git_file),
        ):
            cls.states[label] = build.__func__(cls)
        # Sixty subprocess runs are the expensive part of this class, so the
        # matrix is walked once and every assertion below reads the same
        # recorded results rather than re-running the CLI per question.
        cls.results = {
            (label, command): run_cli(root, *argv)
            for label, root in cls.states.items()
            for command, argv in MATRIX_COMMANDS.items()
        }

    @classmethod
    def tearDownClass(cls):
        for scene in cls._scenes:
            try:
                scene.close()
            except OSError:  # pragma: no cover - cleanup only
                pass
        for directory in cls._temporary:
            try:
                directory.cleanup()
            except OSError:  # pragma: no cover - cleanup only
                pass

    # -- state builders -----------------------------------------------------

    @classmethod
    def _scene(cls) -> Scenario:
        scene = Scenario()
        cls._scenes.append(scene)
        return scene

    @classmethod
    def _scratch(cls) -> Path:
        directory = tempfile.TemporaryDirectory()
        cls._temporary.append(directory)
        return Path(directory.name)

    @classmethod
    def _commit_generated_lock(cls, scene: Scenario, source: str, message: str) -> None:
        """Generate a lockfile from *source* and commit it with everything else."""
        result = run_cli(scene.root, "generate", "--source", source)
        if result.returncode != 0:  # pragma: no cover - fixture failure
            raise AssertionError(
                f"fixture generate --source {source} failed: {result.stderr[:400]}"
            )
        scene.git("add", "--all")
        scene.git("commit", "-m", message)

    @classmethod
    def _committed_scene(cls) -> Scenario:
        """History, a component with a real boundary, and a matching lockfile.

        Three commits rather than one. `verify`, `status` and `review` all read
        the lockfile out of the captured tree, so without a committed one every
        state answers "Lockfile not found in captured head source" and the row
        says nothing about the state. The third commit gives `review` a range
        whose two endpoints both carry a complete lockfile.
        """
        scene = cls._scene()
        scene.component("svc", path="svc", provider="path-hash", boundary=["*.py"])
        scene.file("svc/main.py", "x\n")
        scene.commit()
        cls._commit_generated_lock(scene, "head", "lock")
        scene.file("svc/main.py", "y\n")
        cls._commit_generated_lock(scene, "working-tree", "second")
        return scene

    @classmethod
    def _state_committed(cls) -> Path:
        return cls._committed_scene().root

    @classmethod
    def _state_unborn_empty(cls) -> Path:
        root = cls._scratch()
        init_git_repo(root)
        _write_matrix_config(root)
        return root

    @classmethod
    def _state_unborn_staged(cls) -> Path:
        root = cls._state_unborn_empty()
        _run_git(root, "add", "--all")
        return root

    @classmethod
    def _state_detached(cls) -> Path:
        scene = cls._committed_scene()
        scene.git("checkout", "--detach", "HEAD")
        return scene.root

    @classmethod
    def _state_bare(cls) -> Path:
        root = cls._scratch() / "bare.git"
        subprocess.run(
            ["git", "init", "--bare", str(root)], check=True, capture_output=True
        )
        return root

    @classmethod
    def _state_linked_worktree(cls) -> Path:
        scene = cls._committed_scene()
        linked = cls._scratch() / "linked"
        scene.git("worktree", "add", str(linked), "-b", "linked-branch")
        return linked

    @classmethod
    def _state_shallow(cls) -> Path:
        scene = cls._committed_scene()
        target = cls._scratch() / "shallow"
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--no-local",
                scene.root.as_uri(),
                str(target),
            ],
            check=True,
            capture_output=True,
        )
        return target

    @classmethod
    def _state_gitlink(cls) -> Path:
        scene = cls._scene()
        scene.component("svc", path="svc", provider="path-hash", boundary=["**/*.txt"])
        scene.file("svc/a.txt", "a\n")
        scene.submodule("svc/nested")
        scene.write_config()
        scene.git("add", "boundary.config.json", "svc/a.txt")
        scene.commit_index("gitlink")
        return scene.root

    @classmethod
    def _state_no_refs(cls) -> Path:
        root = cls._scratch()
        init_git_repo(root)
        _write_matrix_config(root)
        shutil.rmtree(root / ".git" / "refs")
        return root

    @classmethod
    def _state_conflicted(cls) -> Path:
        """Both branches carry their own lockfile, so the merge conflicts twice.

        A lockfile per branch is what a real conflicted merge looks like - the
        lock records the content that conflicted - and it is what lets `verify`
        and `status` inspect the mainline HEAD instead of refusing on a missing
        file while the index is unmerged.
        """
        scene = cls._scene()
        scene.component("svc", path="svc", provider="path-hash", boundary=["*.txt"])
        scene.file("svc/c.txt", "base\n")
        scene.commit()
        cls._commit_generated_lock(scene, "head", "lock")
        mainline = scene.current_branch()
        scene.branch("topic")
        scene.file("svc/c.txt", "topic\n")
        cls._commit_generated_lock(scene, "working-tree", "topic")
        scene.checkout(mainline)
        scene.file("svc/c.txt", "mainline\n")
        cls._commit_generated_lock(scene, "working-tree", "mainline")
        subprocess.run(["git", "merge", "topic"], cwd=scene.root, capture_output=True)
        return scene.root

    @classmethod
    def _state_no_git(cls) -> Path:
        root = cls._scratch()
        _write_matrix_config(root)
        return root

    @classmethod
    def _state_dangling_git_file(cls) -> Path:
        root = cls._scratch()
        _write_matrix_config(root)
        (root / ".git").write_text("gitdir: ./absent-gitdir\n", encoding="utf-8")
        return root

    # -- premises about the fixtures ----------------------------------------

    def test_each_state_is_the_git_state_it_claims_to_be(self):
        """The premise for the whole matrix: twelve distinct, real states."""
        self.assertEqual(len(self.states), 12)
        self.assertTrue((self.states["bare-repository"] / "HEAD").is_file())
        self.assertFalse((self.states["bare-repository"] / ".git").exists())
        self.assertTrue(
            (self.states["linked-worktree"] / ".git")
            .read_text(encoding="utf-8")
            .startswith("gitdir:")
        )
        self.assertTrue(
            (self.states["shallow-clone"] / ".git" / "shallow").is_file()
        )
        self.assertIn(
            "160000",
            _run_git(self.states["submodule-gitlink"], "ls-files", "-s").stdout.decode(),
        )
        self.assertFalse((self.states["refs-directory-removed"] / ".git" / "refs").exists())
        self.assertIn(
            "UU",
            _run_git(
                self.states["conflicted-merge"], "status", "--porcelain"
            ).stdout.decode(),
        )
        self.assertIn(
            "boundary.config.json",
            _run_git(self.states["unborn-staged-index"], "ls-files").stdout.decode(),
        )
        self.assertEqual(
            _run_git(self.states["unborn-empty-index"], "ls-files").stdout, b""
        )

    def test_the_traceback_detector_recognises_a_real_traceback(self):
        """The premise for the absence asserted below."""
        crashed = subprocess.run(
            [sys.executable, "-c", "raise ValueError('deliberate')"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(crashed.returncode, 0)
        self.assertIn(TRACEBACK_MARKER, crashed.stderr)

    @staticmethod
    def _committed_lockfile(root: Path) -> Optional[dict]:
        """The lockfile recorded at HEAD, or None when the tree carries none."""
        shown = subprocess.run(
            ["git", "show", "HEAD:boundary.lock.json"],
            cwd=root,
            capture_output=True,
        )
        if shown.returncode != 0:
            return None
        return json.loads(shown.stdout.decode("utf-8"))

    def test_exactly_the_states_that_can_carry_a_lockfile_carry_one(self):
        """The premise that the verify, status and review rows mean anything.

        Without a lockfile in the captured tree those three commands answer
        "Lockfile not found in captured head source" whatever the repository
        looks like, so the row would be a fact about the fixture rather than
        about the state.
        """
        carried = {
            label
            for label, root in self.states.items()
            if self._committed_lockfile(root) is not None
        }
        self.assertEqual(carried, STATES_WITH_A_COMMITTED_LOCKFILE)

    def test_the_committed_lockfile_records_a_resolved_boundary_fingerprint(self):
        """A `leaf` provider would leave this null and resolve nothing."""
        for label in sorted(STATES_WITH_A_COMMITTED_LOCKFILE):
            with self.subTest(state=label):
                fingerprints = self._committed_lockfile(self.states[label])[
                    "components"
                ]["svc"]["fingerprints"]
                self.assertRegex(fingerprints["boundary"], r"\A[0-9a-f]{64}\Z")
                self.assertRegex(fingerprints["exact"], r"\A[0-9a-f]{64}\Z")

    def test_a_stale_lockfile_moves_the_verify_status_and_review_columns(self):
        """The premise for the exit-0 rows: those commands do inspect the tree.

        Every state that carries a lockfile carries one that matches, so its
        three inspection rows all pass. Read alone that is indistinguishable
        from three commands that looked at nothing, so the same three are run
        here against a lockfile the next commit invalidated.
        """
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="path-hash", boundary=["*.py"])
            scene.file("svc/main.py", "x\n")
            scene.commit()
            self._commit_generated_lock(scene, "head", "lock")
            scene.file("svc/main.py", "drifted\n")
            scene.git("add", "--all")
            scene.git("commit", "-m", "drift without regenerating")

            verified = run_cli(scene.root, "verify")
            self.assertEqual(verified.returncode, 4, verified.stderr[:300])
            self.assertIn("MISMATCH svc.boundary:", verified.stdout)
            self.assertIn("MISMATCH svc.exact:", verified.stdout)

            reported = run_cli(scene.root, "status")
            self.assertEqual(reported.returncode, 0, reported.stderr[:300])
            self.assertIn("DRIFT DETECTED (2 issues):", reported.stdout)

            reviewed = run_cli(scene.root, "review", "--base", "HEAD", "--target", "HEAD")
            self.assertEqual(reviewed.returncode, 2, reviewed.stdout[:300])
            # `safe_print` folds the embedded newlines into the two characters
            # `\n`, so the whole refusal arrives as one stderr line.
            self.assertTrue(
                reviewed.stderr.startswith(
                    "ERROR: review failed: Range review compares reconciled "
                    "endpoint commits; base commit "
                ),
                reviewed.stderr[:300],
            )
            self.assertIn("unreconciled drift in 1 component", reviewed.stderr)
            self.assertIn("\\nMISMATCH svc.exact: ", reviewed.stderr)

    # -- the matrix ---------------------------------------------------------

    def test_no_reachable_git_state_lets_any_command_traceback(self):
        self.assertEqual(len(self.results), len(self.states) * len(MATRIX_COMMANDS))
        for (label, command), result in self.results.items():
            with self.subTest(state=label, command=command):
                self.assertNotIn(TRACEBACK_MARKER, result.stderr)
                self.assertNotIn(TRACEBACK_MARKER, result.stdout)

    def test_every_state_yields_a_documented_exit_code_with_a_diagnostic(self):
        """The exit codes are read out of `core`, not restated from memory."""
        documented = {
            value
            for name, value in vars(core).items()
            if name.startswith("EXIT_") and isinstance(value, int)
        }
        self.assertEqual(sorted(documented), [0, 1, 2, 3, 4, 5])
        for (label, command), result in self.results.items():
            with self.subTest(state=label, command=command):
                self.assertIn(
                    result.returncode,
                    documented,
                    f"{label}/{command} exited {result.returncode}: "
                    f"{result.stderr[:200]}",
                )
                if result.returncode != 0:
                    self.assertTrue(
                        result.stderr.startswith("ERROR: "),
                        f"{label}/{command} refused without a diagnostic: "
                        f"{result.stderr[:200]!r}",
                    )

    #: Every cell of the twelve-by-five matrix: its exit code and the exact
    #: first line the command wrote to stderr, or `None` when it wrote nothing.
    #: Recorded from an observed run rather than predicted. Embedded newlines
    #: appear as the two characters `\n` because `core.print` is rebound to
    #: `_output.safe_print`, which folds them.
    MATRIX_OUTCOMES = {
        ("committed-history", "generate"): (0, None),
        ("committed-history", "generate --source working-tree"): (0, None),
        ("committed-history", "verify"): (0, None),
        ("committed-history", "status"): (0, None),
        ("committed-history", "review"): (0, None),
        ("unborn-empty-index", "generate"): (
            2,
            "ERROR: Cannot capture head source: HEAD does not resolve to a commit",
        ),
        ("unborn-empty-index", "generate --source working-tree"): (0, None),
        ("unborn-empty-index", "verify"): (
            2,
            "ERROR: Cannot capture head source: HEAD does not resolve to a commit",
        ),
        ("unborn-empty-index", "status"): (
            2,
            "ERROR: Cannot capture head source: HEAD does not resolve to a commit",
        ),
        ("unborn-empty-index", "review"): (
            2,
            "ERROR: review failed: Cannot capture review endpoints: Cannot "
            "resolve base Git ref 'HEAD' to one commit (return code 128; "
            "stderr='fatal: Needed a single revision').",
        ),
        ("unborn-staged-index", "generate"): (
            2,
            "ERROR: Cannot capture head source: HEAD does not resolve to a commit",
        ),
        ("unborn-staged-index", "generate --source working-tree"): (0, None),
        ("unborn-staged-index", "verify"): (
            2,
            "ERROR: Cannot capture head source: HEAD does not resolve to a commit",
        ),
        ("unborn-staged-index", "status"): (
            2,
            "ERROR: Cannot capture head source: HEAD does not resolve to a commit",
        ),
        ("unborn-staged-index", "review"): (
            2,
            "ERROR: review failed: Cannot capture review endpoints: Cannot "
            "resolve base Git ref 'HEAD' to one commit (return code 128; "
            "stderr='fatal: Needed a single revision').",
        ),
        ("detached-head", "generate"): (0, None),
        ("detached-head", "generate --source working-tree"): (0, None),
        ("detached-head", "verify"): (0, None),
        ("detached-head", "status"): (0, None),
        ("detached-head", "review"): (0, None),
        ("bare-repository", "generate"): (2, "ERROR: Not inside a git repository."),
        ("bare-repository", "generate --source working-tree"): (
            2,
            "ERROR: Not inside a git repository.",
        ),
        ("bare-repository", "verify"): (2, "ERROR: Not inside a git repository."),
        ("bare-repository", "status"): (2, "ERROR: Not inside a git repository."),
        ("bare-repository", "review"): (2, "ERROR: Not inside a git repository."),
        ("linked-worktree", "generate"): (0, None),
        ("linked-worktree", "generate --source working-tree"): (0, None),
        ("linked-worktree", "verify"): (0, None),
        ("linked-worktree", "status"): (0, None),
        ("linked-worktree", "review"): (0, None),
        ("shallow-clone", "generate"): (0, None),
        ("shallow-clone", "generate --source working-tree"): (0, None),
        ("shallow-clone", "verify"): (0, None),
        ("shallow-clone", "status"): (0, None),
        ("shallow-clone", "review"): (0, None),
        ("submodule-gitlink", "generate"): (
            2,
            "ERROR: Lockfile generation failed:\\nsvc: Exact digest failed: "
            "Cannot hash non-blob Git entry at svc/nested: commit mode 160000",
        ),
        ("submodule-gitlink", "generate --source working-tree"): (
            2,
            "ERROR: Lockfile generation failed:\\nsvc: Exact digest failed: "
            "Unsupported working-tree file type at svc/nested",
        ),
        ("submodule-gitlink", "verify"): (
            2,
            "ERROR: Lockfile not found in captured head source: boundary.lock.json",
        ),
        ("submodule-gitlink", "status"): (
            2,
            "ERROR: Lockfile not found in captured head source: boundary.lock.json",
        ),
        ("submodule-gitlink", "review"): (
            2,
            "ERROR: review failed: base endpoint is incomplete: Lockfile not "
            "found in captured head source: boundary.lock.json",
        ),
        ("refs-directory-removed", "generate"): (
            2,
            "ERROR: Not inside a git repository.",
        ),
        ("refs-directory-removed", "generate --source working-tree"): (
            2,
            "ERROR: Not inside a git repository.",
        ),
        ("refs-directory-removed", "verify"): (2, "ERROR: Not inside a git repository."),
        ("refs-directory-removed", "status"): (2, "ERROR: Not inside a git repository."),
        ("refs-directory-removed", "review"): (2, "ERROR: Not inside a git repository."),
        ("conflicted-merge", "generate"): (
            0,
            "WARNING: Could not inspect uncommitted component changes: Cannot "
            "capture index as a complete Git tree: git write-tree failed "
            "(return code 128; stderr='boundary.lock.json: unmerged (",
        ),
        ("conflicted-merge", "generate --source working-tree"): (
            2,
            "ERROR: Config is invalid (1 issues):",
        ),
        ("conflicted-merge", "verify"): (0, None),
        ("conflicted-merge", "status"): (0, None),
        ("conflicted-merge", "review"): (0, None),
        ("no-git-marker", "generate"): (2, "ERROR: Not inside a git repository."),
        ("no-git-marker", "generate --source working-tree"): (
            2,
            "ERROR: Not inside a git repository.",
        ),
        ("no-git-marker", "verify"): (2, "ERROR: Not inside a git repository."),
        ("no-git-marker", "status"): (2, "ERROR: Not inside a git repository."),
        ("no-git-marker", "review"): (2, "ERROR: Not inside a git repository."),
        ("dangling-git-file", "generate"): (2, "ERROR: Not inside a git repository."),
        ("dangling-git-file", "generate --source working-tree"): (
            2,
            "ERROR: Not inside a git repository.",
        ),
        ("dangling-git-file", "verify"): (2, "ERROR: Not inside a git repository."),
        ("dangling-git-file", "status"): (2, "ERROR: Not inside a git repository."),
        ("dangling-git-file", "review"): (2, "ERROR: Not inside a git repository."),
    }

    #: The one cell whose recorded text is a prefix rather than the whole line:
    #: the rest of it is `git write-tree`'s list of unmerged blob ids, which is
    #: a fact about the fixture's own lockfile bytes and not about the state.
    PREFIX_ONLY_CELLS = {("conflicted-merge", "generate")}

    def test_every_cell_of_the_matrix_matches_its_pinned_outcome(self):
        self.assertEqual(sorted(self.MATRIX_OUTCOMES), sorted(self.results))
        for cell, (code, first_line) in self.MATRIX_OUTCOMES.items():
            label, command = cell
            with self.subTest(state=label, command=command):
                result = self.results[cell]
                self.assertEqual(
                    result.returncode,
                    code,
                    f"{label}/{command}: {result.stderr[:300]}{result.stdout[:200]}",
                )
                if first_line is None:
                    self.assertEqual(result.stderr, "")
                elif cell in self.PREFIX_ONLY_CELLS:
                    self.assertTrue(
                        result.stderr.startswith(first_line), result.stderr[:400]
                    )
                else:
                    self.assertEqual(result.stderr.splitlines()[0], first_line)

    def test_the_conflicted_merge_warning_names_both_unmerged_paths(self):
        """The half of that cell the prefix leaves out, asserted by content."""
        stderr = self.results[("conflicted-merge", "generate")].stderr
        self.assertIn("boundary.lock.json: unmerged (", stderr)
        self.assertIn("svc/c.txt: unmerged (", stderr)
        self.assertIn("fatal: git-write-tree: error building trees", stderr)

    def test_every_command_both_succeeds_and_refuses_somewhere_in_the_matrix(self):
        """A column with no successful row never inspected a state at all.

        That is what the lockfile-free version of this matrix produced: verify,
        status and review refused in all twelve states, six of them with the
        identical "Lockfile not found in captured head source" line, so those
        rows recorded the fixture rather than the repository around it. A
        column is only evidence once it has both answers in it.
        """
        for command in MATRIX_COMMANDS:
            with self.subTest(command=command):
                codes = {
                    self.results[(label, command)].returncode for label in self.states
                }
                self.assertIn(0, codes, f"{command} succeeded in no state")
                self.assertTrue(codes - {0}, f"{command} refused in no state")

    def test_the_states_that_hold_a_lockfile_are_the_ones_that_pass_inspection(self):
        """The two halves of the matrix line up, which is the whole point.

        Every state carrying a matching lockfile passes verify, status and
        review; no state without one does. Stated as an equality so a state
        that starts refusing for an unrelated reason cannot hide.
        """
        passing = {
            label
            for label in self.states
            if all(
                self.results[(label, command)].returncode == 0
                for command in ("verify", "status", "review")
            )
        }
        self.assertEqual(passing, STATES_WITH_A_COMMITTED_LOCKFILE)

    def test_a_linked_worktree_produces_the_same_lockfile_as_its_main_worktree(self):
        """A worktree redirection must not change what a component hashes."""
        linked = self.states["linked-worktree"]
        main = Path(
            _run_git(linked, "worktree", "list", "--porcelain")
            .stdout.decode()
            .splitlines()[0]
            .split(" ", 1)[1]
        )
        produced = {}
        for label, root in (("main", main), ("linked", linked)):
            result = run_cli(root, "generate", "--source", "head", "--out", "gen.json")
            self.assertEqual(result.returncode, 0, result.stderr[:300])
            produced[label] = json.loads(
                (root / "gen.json").read_text(encoding="utf-8")
            )["components"]
            (root / "gen.json").unlink()
        self.assertEqual(produced["main"], produced["linked"])
        self.assertRegex(
            produced["linked"]["svc"]["fingerprints"]["boundary"], r"\A[0-9a-f]{64}\Z"
        )

    #: `review` over a real range rather than the degenerate `HEAD..HEAD` the
    #: matrix uses, for the states with two committed endpoints, plus the one
    #: state where the range itself is what fails. Recorded from an observed
    #: run; `None` means the command wrote nothing to stderr.
    REAL_RANGE_REVIEW = {
        "committed-history": (0, None),
        "detached-head": (0, None),
        "linked-worktree": (0, None),
        "conflicted-merge": (0, None),
        "shallow-clone": (
            2,
            "ERROR: review failed: Cannot capture review endpoints: Cannot "
            "resolve base Git ref 'HEAD~1' to one commit (return code 128; "
            "stderr='fatal: Needed a single revision'). Repository is shallow; "
            "fetch complete history first (GitHub Actions: fetch-depth: 0; "
            "GitLab: GIT_DEPTH: 0).",
        ),
    }

    def test_review_compares_two_real_endpoints_where_the_state_has_history(self):
        """The matrix runs `HEAD..HEAD`; this runs a range that has content.

        `HEAD..HEAD` still captures and verifies both endpoints, which is the
        state-dependent work, but it compares a commit with itself. A shallow
        clone is the state where the range is the thing that fails, and its
        refusal carries the fetch-depth remediation, so it is pinned beside the
        four that succeed rather than left out.
        """
        for label, (code, first_line) in self.REAL_RANGE_REVIEW.items():
            with self.subTest(state=label):
                result = run_cli(
                    self.states[label], "review", "--base", "HEAD~1", "--target", "HEAD"
                )
                self.assertEqual(result.returncode, code, result.stderr[:300])
                self.assertNotIn(TRACEBACK_MARKER, result.stderr)
                if first_line is None:
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(
                        result.stdout.splitlines()[:2],
                        ["BOUNDVER RANGE REVIEW", "Range: HEAD~1..HEAD"],
                    )
                else:
                    self.assertEqual(result.stderr.splitlines()[0], first_line)

    def test_the_filesystem_fallback_never_fires_inside_a_repository_with_history(self):
        """The second half of the obligation: no silent approximation."""
        for label in (
            "committed-history",
            "detached-head",
            "linked-worktree",
            "shallow-clone",
            "unborn-staged-index",
        ):
            with self.subTest(state=label):
                accessor = _SourceAccessor(self.states[label], "working-tree")
                try:
                    self.assertIsNotNone(accessor.snapshot)
                finally:
                    accessor.close()

    def test_the_filesystem_fallback_does_fire_for_a_repository_with_no_tracked_state(self):
        """The premise: the assertion above is about the states it names."""
        accessor = _SourceAccessor(self.states["unborn-empty-index"], "working-tree")
        try:
            self.assertIsNone(accessor.snapshot)
        finally:
            accessor.close()


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-061: nothing landing mid-operation may be half-recorded
# ---------------------------------------------------------------------------


class SnapshotAtomicityTests(unittest.TestCase):
    """OBL-GIT-SOURCE-061: one captured tree, or a refusal."""

    def _scene(self) -> Scenario:
        scene = Scenario()
        scene.component("svc", path="svc", provider="path-hash", boundary=["api/*.yaml"])
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.file("svc/api/v2.yaml", "openapi: 3.1.0\nx: 2\n")
        scene.commit()
        return scene

    @staticmethod
    def _fingerprints(lockfile: dict) -> dict:
        return lockfile["components"]["svc"]["fingerprints"]

    def test_a_ref_moving_after_capture_is_absent_from_the_pinned_lock(self):
        with self._scene() as scene:
            before = self._fingerprints(scene.generate())
            captured = _capture_git_source_snapshot(scene.root, "head")
            self.assertEqual(captured.head_oid, scene.head())

            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\nmoved: true\n")
            scene.commit("ref moved")
            self.assertNotEqual(captured.head_oid, scene.head())

            pinned = generate_lockfile(
                scene.config, scene.root, source="head", snapshot=captured
            )
            self.assertEqual(self._fingerprints(pinned), before)

    def test_the_same_repository_without_the_snapshot_does_see_the_moved_ref(self):
        """The premise: the pinning above is pinning, not an unchanged tree."""
        with self._scene() as scene:
            before = self._fingerprints(scene.generate())
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\nmoved: true\n")
            scene.commit("ref moved")
            self.assertNotEqual(self._fingerprints(scene.generate()), before)

    def test_a_commit_landing_between_blob_reads_never_produces_a_hybrid_lock(self):
        with self._scene() as scene:
            before = self._fingerprints(scene.generate())
            landed: List[str] = []
            original = git._GitBlobSession.read_blob

            def read_then_commit(session, oid, max_bytes=None):
                if not landed:
                    landed.append(oid)
                    scene.file("svc/api/v1.yaml", "openapi: 3.1.0\nMUTATED\n")
                    scene.file("svc/api/v2.yaml", "openapi: 3.1.0\nMUTATED\n")
                    scene.commit("landed mid-read")
                if max_bytes is None:
                    return original(session, oid)
                return original(session, oid, max_bytes=max_bytes)

            with mock.patch.object(
                git._GitBlobSession, "read_blob", read_then_commit
            ):
                during = self._fingerprints(
                    generate_lockfile(scene.config, scene.root, source="head")
                )
            self.assertTrue(landed, "the injected commit never ran")
            after = self._fingerprints(scene.generate())
            self.assertNotEqual(before, after)
            self.assertIn(
                during,
                (before, after),
                "generate produced a tree that never existed",
            )
            self.assertEqual(during, before)

    # -- the working-tree ancestor window -----------------------------------

    def test_capture_working_tree_ancestors_records_every_plain_directory(self):
        with self._scene() as scene:
            ancestors = _capture_working_tree_ancestors(
                scene.root, scene.root / "svc" / "api" / "v1.yaml", "svc/api/v1.yaml"
            )
            self.assertEqual(
                [path.relative_to(scene.root).as_posix() for path, _ in ancestors],
                ["svc", "svc/api"],
            )

    def test_an_untouched_ancestor_chain_verifies(self):
        """The premise: the verifier accepts what it was handed unchanged."""
        with self._scene() as scene:
            ancestors = _capture_working_tree_ancestors(
                scene.root, scene.root / "svc" / "api" / "v1.yaml", "svc/api/v1.yaml"
            )
            _verify_working_tree_ancestors(ancestors, "svc/api/v1.yaml")

    def test_a_removed_or_replaced_ancestor_directory_aborts_the_read(self):
        cases = ("removed", "replaced")
        for case in cases:
            with self.subTest(ancestor=case):
                with self._scene() as scene:
                    full = scene.root / "svc" / "api" / "v1.yaml"
                    body = full.read_bytes()
                    ancestors = _capture_working_tree_ancestors(
                        scene.root, full, "svc/api/v1.yaml"
                    )
                    shutil.rmtree(scene.root / "svc" / "api")
                    if case == "replaced":
                        (scene.root / "svc" / "api").mkdir(parents=True)
                        full.write_bytes(body)
                    with self.assertRaises(ValueError) as raised:
                        _verify_working_tree_ancestors(ancestors, "svc/api/v1.yaml")
                    self.assertEqual(
                        str(raised.exception),
                        "File changed while hashing: svc/api/v1.yaml",
                    )

    def test_a_missing_ancestor_is_refused_at_capture_time(self):
        with self._scene() as scene:
            full = scene.root / "svc" / "api" / "v1.yaml"
            shutil.rmtree(scene.root / "svc" / "api")
            with self.assertRaises(ValueError) as raised:
                _capture_working_tree_ancestors(scene.root, full, "svc/api/v1.yaml")
            self.assertEqual(
                str(raised.exception),
                "File disappeared while hashing: svc/api/v1.yaml",
            )

    def test_a_path_outside_the_repository_is_refused_at_capture_time(self):
        with self._scene() as scene:
            with self.assertRaises(ValueError) as raised:
                _capture_working_tree_ancestors(
                    scene.root, scene.root.parent / "outside.txt", "outside.txt"
                )
            self.assertEqual(str(raised.exception), "Path escapes repository: outside.txt")


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-118: the -z name-status record parser against git itself
# ---------------------------------------------------------------------------


#: Filenames that are adversarial for `-z` record framing and still legal on
#: every host the suite runs on. Double quotes, newlines, tabs, `*` and `\`
#: are refused by Git itself on Windows - `update-index` answers "Invalid path"
#: for `--cacheinfo` and "Ignoring path" for `--index-info` - so those shapes
#: are exercised through synthetic field streams further down instead.
ADVERSARIAL_NAMES = (
    "plain.txt",
    "a space.txt",
    "a'quote.txt",
    "semi;colon.txt",
    "dollar$sign.txt",
    "é中.txt",
    "UPPER.txt",
    "upper.txt",
    "deep/dir with space/leaf.txt",
)

#: Blob bodies the property draws from. Two entries sharing a body is what
#: makes Git's rename detection fire, which is the case where one record
#: carries two paths.
CONTENT_POOL = (b"alpha\n", b"beta\n", b"gamma" * 20 + b"\n", b"")

#: File modes the property draws from. 160000 is excluded: a gitlink needs a
#: real commit id, and its absence is recorded in this file's residual gap.
MODE_POOL = ("100644", "100755", "120000")

PARSER_PROFILE = settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

Entry = Tuple[str, str]


class NameStatusParserTests(unittest.TestCase):
    """OBL-GIT-SOURCE-118: every status letter, and git's own path set."""

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario()
        cls.scene.component("svc", path="svc", provider="leaf")
        cls.scene.file("svc/seed.txt", "seed\n")
        cls.scene.commit()
        cls.blobs = {
            content: _hash_blob(cls.scene.root, content) for content in CONTENT_POOL
        }

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()

    # -- helpers ------------------------------------------------------------

    @classmethod
    def _commit_tree(cls, entries: Dict[str, Entry]) -> str:
        """Write *entries* as one tree and return the commit that carries it."""
        root = cls.scene.root
        _run_git(root, "read-tree", "--empty")
        if entries:
            payload = b"".join(
                f"{mode} {oid}\t{path}".encode("utf-8") + b"\0"
                for path, (mode, oid) in sorted(entries.items())
            )
            _run_git(root, "update-index", "-z", "--add", "--index-info", input=payload)
        tree = _run_git(root, "write-tree").stdout.decode().strip()
        return _run_git(root, "commit-tree", tree, "-m", "generated").stdout.decode().strip()

    @staticmethod
    def _diff_fields(root: Path, base: str, target: str, *flags: str) -> List[bytes]:
        """The raw `-z` field stream, split exactly the way the reader does."""
        raw = subprocess.run(
            ["git", "diff", *flags, "--name-status", "-z", base, target, "--"],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
        return [field for field in raw.split(b"\0") if field != b""]

    # -- the property -------------------------------------------------------

    @given(
        base=st.dictionaries(
            st.sampled_from(ADVERSARIAL_NAMES),
            st.tuples(st.sampled_from(MODE_POOL), st.sampled_from(CONTENT_POOL)),
            max_size=6,
        ),
        target=st.dictionaries(
            st.sampled_from(ADVERSARIAL_NAMES),
            st.tuples(st.sampled_from(MODE_POOL), st.sampled_from(CONTENT_POOL)),
            max_size=6,
        ),
    )
    @example(
        base={"a space.txt": ("100644", b"alpha\n"), "UPPER.txt": ("100755", b"beta\n")},
        target={"a'quote.txt": ("100644", b"alpha\n"), "UPPER.txt": ("120000", b"beta\n")},
    )
    @PARSER_PROFILE
    def test_the_parser_reproduces_the_changed_path_set_git_reports(self, base, target):
        """The oracle is the fixture's own two trees, not another parser.

        A path is changed exactly when the (mode, blob) pair recorded for it
        differs between the two trees, which the test knows because it chose
        both. Git's rename detection may fold a delete and an add into one
        record, but such a record contributes its source *and* its destination,
        so the set is unaffected.
        """
        base_entries = {
            path: (mode, self.blobs[content]) for path, (mode, content) in base.items()
        }
        target_entries = {
            path: (mode, self.blobs[content]) for path, (mode, content) in target.items()
        }
        expected = {
            path
            for path in set(base_entries) | set(target_entries)
            if base_entries.get(path) != target_entries.get(path)
        }
        base_commit = self._commit_tree(base_entries)
        target_commit = self._commit_tree(target_entries)
        fields = self._diff_fields(self.scene.root, base_commit, target_commit)
        entries = _parse_name_status_entries(iter(fields))
        self.assertEqual({path for _status, path in entries}, expected)

    def test_the_property_comparison_is_sensitive_to_the_record_stream(self):
        """The premise: the equality above can fail, and does when a record goes.

        Without this the property would be satisfied by a parser that returned
        the empty set for everything and an oracle that happened to be empty.
        """
        alpha = self.blobs[b"alpha\n"]
        beta = self.blobs[b"beta\n"]
        base_commit = self._commit_tree(
            {"one.txt": ("100644", alpha), "two.txt": ("100644", alpha)}
        )
        target_commit = self._commit_tree(
            {"one.txt": ("100644", beta), "two.txt": ("100644", beta)}
        )
        fields = self._diff_fields(
            self.scene.root, base_commit, target_commit, "--no-renames"
        )
        self.assertEqual(len(fields), 4)
        self.assertEqual(
            {path for _status, path in _parse_name_status_entries(iter(fields))},
            {"one.txt", "two.txt"},
        )
        self.assertEqual(
            {path for _status, path in _parse_name_status_entries(iter(fields[:2]))},
            {"one.txt"},
        )

    # -- every status letter ------------------------------------------------

    def test_add_delete_modify_and_typechange_come_out_of_real_git_output(self):
        """The four letters the production `--no-renames` diff can produce."""
        alpha = self.blobs[b"alpha\n"]
        beta = self.blobs[b"beta\n"]
        base_commit = self._commit_tree(
            {
                "keep.txt": ("100644", alpha),
                "mod.txt": ("100644", alpha),
                "del.txt": ("100644", beta),
                "type.txt": ("100644", alpha),
            }
        )
        target_commit = self._commit_tree(
            {
                "keep.txt": ("100644", alpha),
                "mod.txt": ("100644", beta),
                "add.txt": ("100644", beta),
                "type.txt": ("120000", alpha),
            }
        )
        fields = self._diff_fields(
            self.scene.root, base_commit, target_commit, "--no-renames"
        )
        entries = _parse_name_status_entries(iter(fields))
        self.assertEqual(
            sorted(entries),
            [
                ("A", "add.txt"),
                ("D", "del.txt"),
                ("M", "mod.txt"),
                ("T", "type.txt"),
            ],
        )

    def test_a_rename_record_contributes_its_source_and_its_destination(self):
        """The one shape where a single record carries two paths."""
        body = self.blobs[b"gamma" * 20 + b"\n"]
        base_commit = self._commit_tree({"ren src.txt": ("100644", body)})
        target_commit = self._commit_tree({"ren dst.txt": ("100644", body)})
        fields = self._diff_fields(
            self.scene.root, base_commit, target_commit, "--find-renames"
        )
        self.assertEqual(fields[0], b"R100")
        self.assertEqual(
            _parse_name_status_entries(iter(fields)),
            [("R100", "ren src.txt"), ("R100", "ren dst.txt")],
        )

    def test_a_rename_contributes_both_paths_and_a_copy_only_its_destination(self):
        body = "".join(f"line {number}\n" for number in range(40))
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/origin.txt", body)
            scene.file("svc/other.txt", "other\n")
            scene.commit()
            base = scene.head()
            scene.file("svc/copy.txt", body)
            scene.file("svc/other.txt", "other changed\n")
            scene.commit("copy")
            raw = scene.git_bytes(
                "diff-tree",
                "-r",
                "-C",
                "--find-copies-harder",
                "--name-status",
                "-z",
                base,
                "HEAD",
            )
            fields = [field for field in raw.split(b"\0") if field != b""]
            self.assertIn(b"C100", fields)
            entries = _parse_name_status_entries(iter(fields))
            self.assertEqual(
                sorted(entries),
                [("C100", "svc/copy.txt"), ("M", "svc/other.txt")],
            )

    def test_an_unmerged_index_reports_U_for_the_conflicted_path(self):
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/c.txt", "base\n")
            scene.commit()
            mainline = scene.current_branch()
            scene.branch("topic")
            scene.file("svc/c.txt", "topic\n")
            scene.commit("topic")
            scene.checkout(mainline)
            scene.file("svc/c.txt", "mainline\n")
            scene.commit("mainline")
            merged = subprocess.run(
                ["git", "merge", "topic"], cwd=scene.root, capture_output=True
            )
            self.assertEqual(merged.returncode, 1)
            raw = scene.git_bytes("diff", "--cached", "--name-status", "-z")
            fields = [field for field in raw.split(b"\0") if field != b""]
            self.assertEqual(fields[0], b"U")
            self.assertEqual(
                _parse_name_status_entries(iter(fields)), [("U", "svc/c.txt")]
            )

    #: Status letters `git diff` will not emit here, and path shapes Git on
    #: Windows refuses to store, driven through the parser as field streams.
    #: `B` is in the parser's accepted alphabet but Git 2.55 answers a total
    #: rewrite with `M100`; `X` means "unknown" and no Git emits it.
    SYNTHETIC_STREAMS = {
        "broken pairing": ([b"B", b"svc/rewrite.txt"], [("B", "svc/rewrite.txt")]),
        "unknown": ([b"X", b"svc/odd.txt"], [("X", "svc/odd.txt")]),
        "newline in path": (
            [b"M", b"svc/two\nlines.txt"],
            [("M", "svc/two\nlines.txt")],
        ),
        "double quote in path": (
            [b"A", b'svc/a"quote.txt'],
            [("A", 'svc/a"quote.txt')],
        ),
        "tab in path": ([b"D", b"svc/a\ttab.txt"], [("D", "svc/a\ttab.txt")]),
        "score suffix": ([b"M100", b"svc/m.txt"], [("M100", "svc/m.txt")]),
        "rename with a quoted destination": (
            [b"R090", b"svc/old name.txt", b'svc/new"name.txt'],
            [("R090", "svc/old name.txt"), ("R090", 'svc/new"name.txt')],
        ),
        "copy with a spaced destination": (
            [b"C075", b"svc/src.txt", b"svc/dst copy.txt"],
            [("C075", "svc/dst copy.txt")],
        ),
    }

    def test_the_shapes_git_will_not_produce_here_still_parse_correctly(self):
        for label, (fields, expected) in self.SYNTHETIC_STREAMS.items():
            with self.subTest(stream=label):
                self.assertEqual(_parse_name_status_entries(iter(fields)), expected)

    def test_git_on_this_host_really_refuses_the_path_shapes_above(self):
        """The premise for using synthetic streams rather than a repository."""
        if os.name != "nt":
            self.skipTest("Git only rejects these pathnames on Windows")
        blob = self.blobs[b"alpha\n"]
        for label, raw in (
            ("double quote", b'svc/a"quote.txt'),
            ("newline", b"svc/a\nnewline.txt"),
            ("tab", b"svc/a\ttab.txt"),
        ):
            with self.subTest(name=label):
                refused = subprocess.run(
                    ["git", "update-index", "-z", "--add", "--index-info"],
                    cwd=self.scene.root,
                    input=b"100644 " + blob.encode() + b"\t" + raw + b"\0",
                    capture_output=True,
                )
                self.assertIn(b"Ignoring path", refused.stderr)

    #: Every malformed stream shape and the message it must produce.
    MALFORMED_STREAMS = {
        "status with no path": ([b"M"], "Truncated path in Git diff output"),
        "rename with no destination": (
            [b"R100", b"svc/old.txt"],
            "Truncated rename/copy in Git diff output",
        ),
        "copy with no destination": (
            [b"C100", b"svc/old.txt"],
            "Truncated rename/copy in Git diff output",
        ),
        "empty status": ([b"", b"svc/a.txt"], "Malformed Git diff status: ''"),
        "unknown status letter": (
            [b"Z", b"svc/a.txt"],
            "Malformed Git diff status: 'Z'",
        ),
        "empty path": ([b"M", b""], "Malformed empty path in Git output"),
        "non-ascii status": (
            [b"\xff", b"svc/a.txt"],
            "Malformed non-ASCII Git diff status",
        ),
    }

    def test_a_truncated_or_malformed_record_stream_raises(self):
        for label, (fields, message) in self.MALFORMED_STREAMS.items():
            with self.subTest(stream=label):
                with self.assertRaises(ValueError) as raised:
                    _parse_name_status_entries(iter(fields))
                self.assertIn(message, str(raised.exception))

    # -- undecodable pathnames ----------------------------------------------

    def _undecodable_repository(self, scene: Scenario) -> Tuple[str, str, bytes]:
        """Commit one path whose bytes are not valid UTF-8, via the index."""
        base = scene.head()
        blob = _hash_blob(scene.root, b"undecodable\n")
        raw = b"svc/bad\xff.txt"
        _run_git(
            scene.root,
            "update-index",
            "-z",
            "--add",
            "--index-info",
            input=b"100644 " + blob.encode() + b"\t" + raw + b"\0",
        )
        tree = _run_git(scene.root, "write-tree").stdout.decode().strip()
        commit = (
            _run_git(scene.root, "commit-tree", tree, "-p", base, "-m", "bad")
            .stdout.decode()
            .strip()
        )
        return base, commit, raw

    def test_an_undecodable_pathname_is_reproduced_or_refused_as_a_value_error(self):
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/seed.txt", "seed\n")
            scene.commit()
            base, commit, raw = self._undecodable_repository(scene)
            fields = self._diff_fields(scene.root, base, commit)
            self.assertEqual(fields, [b"A", raw])
            try:
                entries = _parse_name_status_entries(iter(fields))
            except ValueError as exc:
                self.assertIsInstance(exc, UnicodeDecodeError)
                self.assertEqual(
                    os.name,
                    "nt",
                    "a POSIX host surrogate-escapes rather than refusing",
                )
            else:
                self.assertEqual(
                    [path for _status, path in entries], [os.fsdecode(raw)]
                )

    def test_the_refusal_is_a_value_error_so_the_cli_diagnoses_it(self):
        """Pin the blast radius: `except ValueError` in core.py catches this."""
        if os.name != "nt":
            self.skipTest("only Windows cannot spell an undecodable pathname")
        self.assertTrue(issubclass(UnicodeDecodeError, ValueError))
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/seed.txt", "seed\n")
            scene.commit()
            base, commit, _raw = self._undecodable_repository(scene)
            _run_git(scene.root, "reset", "--hard", commit)
            with self.assertRaises(ValueError):
                changed_paths_since_ref(scene.root, base, source="head")


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-136: which reader exceptions are absorbed, and what it costs
# ---------------------------------------------------------------------------


#: Every subcommand this file drives against a poisoned version source. The
#: obligation names `verify --status`; the parser registers no such flag, and
#: `status` is a subcommand in its own right, so it is spelled that way here.
VERSION_READING_COMMANDS = {
    "generate": ("generate", "--source", "head"),
    "verify": ("verify", "--source", "head"),
    "status": ("status", "--source", "head"),
    "explain": ("explain", "a"),
    "why": ("why", "a"),
    "review": ("review", "--base", "HEAD~1", "--target", "HEAD"),
}

#: Subcommands that create, mutate or describe rather than measure a component,
#: and so are deliberately not driven here. The union of the two sets is
#: compared against the parser at runtime, so a new subcommand fails this file
#: until somebody decides which side it belongs on.
NON_MEASURING_COMMANDS = {
    "add",
    "check-config",
    "completions",
    "coverage",
    "diff",
    "discover",
    "init",
    "migrate-lock",
    "record-derivation",
    "remove",
    "slice",
    "validate-config",
}


#: The exact first stderr line each command produces in the symlink-entry
#: scene, and the reason the scene cannot exercise the reader: three commands
#: refuse at config validation, two on the missing lockfile, one on the base
#: ref. Recorded from an observed run, all six at exit 2.
SYMLINK_SCENE_REFUSALS = {
    "generate": "ERROR: Config is invalid (1 issues):",
    "verify": "ERROR: Config is invalid (1 issues):",
    "explain": "ERROR: Config is invalid (1 issues):",
    "status": (
        "ERROR: Lockfile not found in captured head source: boundary.lock.json"
    ),
    "why": "ERROR: Lockfile not found in captured head source: boundary.lock.json",
    "review": (
        "ERROR: review failed: Cannot capture review endpoints: Cannot resolve "
        "base Git ref 'HEAD~1' to one commit (return code 128; stderr='fatal: "
        "Needed a single revision')."
    ),
}

#: A program that replaces `version_read_file` with one raising the bare
#: `ValueError` a mid-read race raises, then runs the real CLI entry point. It
#: has to be a subprocess: `run_cli_in_process` would let an unhandled
#: exception escape into the test rather than into a user's terminal, and
#: whether the user sees a traceback is the thing being measured.
_POISONED_READER_PROGRAM = (
    "from boundver._lockfile import _SourceAccessor\n"
    "from boundver.cli import main\n"
    "def poisoned(self, repo_rel):\n"
    "    raise ValueError('File changed while hashing: ' + repo_rel)\n"
    "_SourceAccessor.version_read_file = poisoned\n"
    "main()\n"
)

#: The same entry point with the reader left alone: the premise run.
_HEALTHY_READER_PROGRAM = "from boundver.cli import main\nmain()\n"

#: What each command does with that error after CLI normalization.
POISONED_READER_OUTCOMES = {
    "generate": (
        2,
        [
            "ERROR: File changed while hashing: a/meta.json",
            "Review the reported provider, source, or facet error. Use "
            "--allow-partial only when null slice facet inputs are intentional.",
        ],
    ),
    "verify": (2, ["ERROR: File changed while hashing: a/meta.json"]),
    "status": (
        0,
        [],
    ),
    "explain": (0, []),
    "why": (
        2,
        [
            "ERROR: could not compute current fingerprints: File changed while "
            "hashing: a/meta.json"
        ],
    ),
    "review": (
        2,
        ["ERROR: review failed: File changed while hashing: a/meta.json"],
    ),
}

_SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")


def _run_cli_program(
    program: str, root: Path, *args: str
) -> subprocess.CompletedProcess:
    """Run *program* as `python -c` in *root*, with `src` on the import path."""
    env = os.environ.copy()
    env["PYTHONPATH"] = _SRC_DIR + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return subprocess.run(
        [sys.executable, "-c", program, *args],
        cwd=root,
        capture_output=True,
        text=True,
        env=env,
    )


def _registered_subcommands() -> List[str]:
    """The CLI surface, read from the parser production `main` builds."""
    parser = build_parser(version="0.0.0", epilog="")
    for action in parser._subparsers._group_actions:  # noqa: SLF001
        return sorted(action.choices)
    raise AssertionError("the CLI parser registered no subcommands")


def _absorbed_exception_names() -> List[str]:
    """Read the except clause `extract_version` uses, rather than restating it.

    The obligation is that this set and the set the production reader raises
    are deliberately aligned. Restating the tuple here would make the test
    agree with itself, so it is parsed out of the function's own source.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(versions.extract_version)))
    names: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        candidates = (
            node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
        )
        collected = []
        for candidate in candidates:
            collected.append(
                ast.unparse(candidate) if hasattr(ast, "unparse") else candidate.id
            )
        # The reader-facing handler is the one guarding `read_file_fn`; the
        # other handlers in this function guard path normalisation instead.
        if "GuardrailError" in collected:
            names = collected
    if not names:
        raise AssertionError("no reader handler found in extract_version")
    return names


def _resolve(name: str) -> type:
    """Resolve one dotted exception name the way the reader's module sees it."""
    head, _, rest = name.partition(".")
    value = getattr(versions, head, None)
    if value is None:
        value = getattr(builtins, head)
    for part in rest.split(".") if rest else ():
        value = getattr(value, part)
    assert isinstance(value, type), name
    return value


class VersionReaderTaxonomyTests(unittest.TestCase):
    """OBL-GIT-SOURCE-136: the reader's exception surface and its blast radius."""

    def setUp(self):
        # One test runs the CLI inside this interpreter, which populates the
        # two path-keyed Git config caches with disposable repository roots.
        self._clear_git_caches()

    def tearDown(self):
        self._clear_git_caches()

    @staticmethod
    def _clear_git_caches() -> None:
        git._ambient_worktree_config_overrides.cache_clear()
        git._repository_filter_config_overrides.cache_clear()

    @staticmethod
    def _scene(version_file: str = "meta.json") -> Scenario:
        scene = Scenario()
        for name in ("a", "b"):
            scene.component(
                name,
                path=name,
                provider="path-hash",
                boundary=["*.json"],
                version_source={
                    "file": version_file if name == "a" else "meta.json",
                    "field": "version",
                },
            )
        scene.json_file("a/meta.json", {"version": "1.0.0"})
        scene.json_file("b/meta.json", {"version": "2.0.0"})
        return scene

    # -- the absorbing split is read, not restated --------------------------

    def test_the_absorbed_exception_set_is_read_from_the_reader_itself(self):
        names = _absorbed_exception_names()
        self.assertEqual(
            sorted(names), ["GuardrailError", "OSError", "subprocess.CalledProcessError"]
        )
        for name in names:
            with self.subTest(exception=name):
                self.assertTrue(issubclass(_resolve(name), BaseException))

    def test_extract_version_absorbs_exactly_the_types_that_set_names(self):
        absorbed = tuple(_resolve(name) for name in _absorbed_exception_names())
        candidates = {
            "OSError": OSError("boom"),
            "CalledProcessError": subprocess.CalledProcessError(1, ["git"]),
            "GuardrailError": GuardrailError("boom"),
            "ConfigError": ConfigError("boom"),
            "ValueError": ValueError("boom"),
        }
        with self._scene() as scene:
            scene.commit()
            for label, error in candidates.items():
                with self.subTest(raised=label):
                    def failing_reader(_repo_rel, _error=error):
                        raise _error

                    expected_absorbed = isinstance(error, absorbed)
                    if expected_absorbed:
                        self.assertIsNone(
                            extract_version(
                                scene.root,
                                "a",
                                {"file": "meta.json", "field": "version"},
                                read_file_fn=failing_reader,
                            )
                        )
                    else:
                        with self.assertRaises(type(error)):
                            extract_version(
                                scene.root,
                                "a",
                                {"file": "meta.json", "field": "version"},
                                read_file_fn=failing_reader,
                            )

    def test_a_healthy_version_source_is_read_rather_than_absorbed(self):
        """The premise: `None` above means refusal, not a reader that never ran."""
        with self._scene() as scene:
            scene.commit()
            self.assertEqual(
                {
                    name: entry.get("version")
                    for name, entry in scene.generate()["components"].items()
                },
                {"a": "1.0.0", "b": "2.0.0"},
            )

    # -- what the production reader actually raises -------------------------

    def _symlink_entry_scene(self) -> Scenario:
        """A version source that is a 120000 index entry, without a symlink.

        This host cannot create a symlink, and the accessor's refusal is keyed
        on the captured Git mode rather than on a filesystem probe, so writing
        the index entry directly reaches the same branch on every platform.
        """
        scene = self._scene(version_file="link.json")
        scene.write_config()
        scene.git("add", "--all")
        oid = _hash_blob(scene.root, b"meta.json")
        scene.git("update-index", "--add", "--cacheinfo", f"120000,{oid},a/link.json")
        scene.commit_index("symlink entry")
        return scene

    def _gitlink_entry_scene(self) -> Scenario:
        """A version source pointing at a submodule gitlink: a non-blob entry."""
        scene = self._scene(version_file="nested.json")
        scene.submodule("a/nested.json")
        scene.write_config()
        scene.git("add", "boundary.config.json", "a/meta.json", "b/meta.json")
        scene.commit_index("gitlink")
        return scene

    def test_each_reader_failure_mode_raises_the_type_the_taxonomy_records(self):
        cases = {
            "absent from the captured tree": (
                self._scene,
                "head",
                "a/absent.json",
                ValueError,
                "Path is absent from captured head tree: a/absent.json",
            ),
            "symlink index entry (head)": (
                self._symlink_entry_scene,
                "head",
                "a/link.json",
                ConfigError,
                "Version source must not be a symlink: a/link.json",
            ),
            "symlink index entry (index)": (
                self._symlink_entry_scene,
                "index",
                "a/link.json",
                ConfigError,
                "Version source must not be a symlink: a/link.json",
            ),
            "non-blob captured entry": (
                self._gitlink_entry_scene,
                "head",
                "a/nested.json",
                ValueError,
                "Expected Git blob at a/nested.json, got commit mode 160000",
            ),
            "untracked working-tree path": (
                self._scene,
                "working-tree",
                "a/untracked.json",
                ConfigError,
                "Version source is not tracked in the captured index: a/untracked.json",
            ),
        }
        for label, (build, source, repo_rel, kind, message) in cases.items():
            with self.subTest(failure=label):
                with build() as scene:
                    if "boundary.config.json" not in scene.git("ls-files"):
                        scene.commit()
                    accessor = _SourceAccessor(scene.root, source)
                    try:
                        with self.assertRaises(kind) as raised:
                            accessor.version_read_file(repo_rel)
                    finally:
                        accessor.close()
                    self.assertEqual(str(raised.exception), message)
                    self.assertEqual(type(raised.exception), kind)

    def test_a_config_error_is_a_value_error_but_not_a_guardrail_error(self):
        """The structural fact the whole split rests on.

        Both errors sit under `BoundverError`, which is not itself a
        `ValueError`; each of the two leaves adds `ValueError` separately. So
        `except GuardrailError` absorbs one sibling and not the other, while
        `except ValueError` in the CLI catches both plus the bare kind.
        """
        self.assertTrue(issubclass(ConfigError, BoundverError))
        self.assertTrue(issubclass(GuardrailError, BoundverError))
        self.assertFalse(issubclass(BoundverError, ValueError))
        self.assertTrue(issubclass(ConfigError, ValueError))
        self.assertTrue(issubclass(GuardrailError, ValueError))
        self.assertFalse(issubclass(ConfigError, GuardrailError))

    # -- blast radius, measured rather than assumed -------------------------

    def _oversized_scene_issues(self, oversized: Tuple[str, ...], drift_b: bool = False):
        """Break the named components' version files and verify against a good lock."""
        with self._scene() as scene:
            scene.commit()
            healthy = scene.generate()
            self.assertEqual(
                {
                    name: entry.get("version")
                    for name, entry in healthy["components"].items()
                },
                {"a": "1.0.0", "b": "2.0.0"},
            )
            padding = "x" * (MAX_VERSION_FILE_BYTES + 64)
            for name in oversized:
                version = {"a": "1.0.0", "b": "2.0.0"}[name]
                scene.file(
                    f"{name}/meta.json",
                    '{"version": "' + version + '", "pad": "' + padding + '"}',
                )
            if drift_b:
                scene.json_file("b/x.json", {"drifted": True})
            scene.commit("oversized")
            issues = verify_lockfile(
                scene.config,
                healthy,
                scene.root,
                source="head",
                components_filter=["a", "b"],
            )
        return [_elide_digests(issue) for issue in issues]

    #: The seven lines one broken version source contributes, as a template on
    #: the component name. Six of them are consequences of the file's own bytes
    #: changing; the first is the version reader refusing.
    BROKEN_VERSION_ROWS = (
        "CURRENT DIGEST ERROR {name}: Configured version source did not produce a "
        "version (file 'meta.json', field 'version')",
        "MISMATCH {name}.compat: lockfile=<digest> current=none",
        "MISMATCH {name}.boundary: lockfile=<digest> current=<digest>",
        "MISMATCH {name}.exact: lockfile=<digest> current=<digest>",
        "METADATA MISMATCH {name}.version: lockfile='{version}' current=None",
        "METADATA MISMATCH {name}.semver: lockfile={{'compat_family': '{family}', "
        "'api_surface': '{family}.0', 'exact_version': '{version}'}} "
        "current={{'compat_family': None, 'api_surface': None, 'exact_version': None}}",
        "METADATA MISMATCH {name}.version_errors: lockfile=None "
        "current=[\"Configured version source did not produce a version "
        "(file 'meta.json', field 'version')\"]",
    )

    @classmethod
    def _broken_rows(cls, name: str, version: str) -> List[str]:
        return [
            row.format(name=name, version=version, family=version.split(".")[0])
            for row in cls.BROKEN_VERSION_ROWS
        ]

    def test_an_oversized_version_file_degrades_one_component_under_verify(self):
        """Assert the whole list, so the healthy component's silence is checked.

        The earlier spelling of this test filtered for `" b" in issue[:20]`,
        which cannot see `'CURRENT DIGEST ERROR b: ...'` at all - the prefix is
        exactly twenty characters long, so the slice stops before the component
        name. Comparing the entire list removes the question.
        """
        self.assertEqual(
            self._oversized_scene_issues(("a",)), self._broken_rows("a", "1.0.0")
        )

    def test_the_same_verify_reports_the_second_component_when_it_is_broken_too(self):
        """The premise for the absence above: nothing suppresses `b`'s rows."""
        self.assertEqual(
            self._oversized_scene_issues(("a", "b")),
            self._broken_rows("a", "1.0.0") + self._broken_rows("b", "2.0.0"),
        )

    def test_verify_examines_the_healthy_component_beside_the_broken_one(self):
        """Positive evidence that `b` was measured, not skipped.

        An empty result for `b` is what you would see either way. Drifting `b`
        while `a` stays broken makes `b`'s own mismatches appear in the same
        list, which only a component that was actually hashed can produce.
        """
        self.assertEqual(
            self._oversized_scene_issues(("a",), drift_b=True),
            self._broken_rows("a", "1.0.0")
            + [
                "MISMATCH b.boundary: lockfile=<digest> current=<digest>",
                "MISMATCH b.exact: lockfile=<digest> current=<digest>",
            ],
        )

    def test_an_oversized_version_file_still_aborts_generate_entirely(self):
        """The register's blast-radius claim is only half true, and this is why.

        `_generation_errors` promotes a `version_errors` entry into an
        operation-wide failure, and `strict=False` is documented not to relax
        it, so under `generate` an absorbed GuardrailError costs the same as an
        unabsorbed ConfigError.
        """
        with self._scene() as scene:
            scene.commit()
            scene.file(
                "a/meta.json",
                '{"version": "1.0.0", "pad": "' + "x" * (MAX_VERSION_FILE_BYTES + 64) + '"}',
            )
            scene.commit("oversized")
            for strict in (True, False):
                with self.subTest(strict=strict):
                    with self.assertRaises(ConfigError) as raised:
                        generate_lockfile(
                            scene.config, scene.root, source="head", strict=strict
                        )
                    self.assertEqual(
                        str(raised.exception),
                        "Lockfile generation failed:\n"
                        "a: Configured version source did not produce a version "
                        "(file 'meta.json', field 'version')",
                    )

    def test_an_unabsorbed_reader_error_aborts_the_whole_generate(self):
        with self._symlink_entry_scene() as scene:
            with self.assertRaises(ConfigError) as raised:
                generate_lockfile(scene.config, scene.root, source="head")
            self.assertEqual(
                str(raised.exception),
                "Version source must not be a symlink: a/link.json",
            )

    # -- the command surface ------------------------------------------------

    def test_every_registered_subcommand_is_classified_by_this_file(self):
        self.assertEqual(
            sorted(set(VERSION_READING_COMMANDS) | NON_MEASURING_COMMANDS),
            _registered_subcommands(),
        )

    def _prepare_reader_reachable(self, scene: Scenario) -> Dict[str, Tuple[str, ...]]:
        """Commit a lockfile and two review endpoints, and return the argv table.

        Every command in `VERSION_READING_COMMANDS` refuses before it reaches
        `version_read_file` unless the tree carries a committed lockfile, and
        `review` refuses unless both of its endpoints carry one, so this is the
        minimum a scene needs before the reader can be observed or replaced.
        """
        scene.commit()
        argv_by_command = dict(VERSION_READING_COMMANDS)
        self.assertEqual(
            run_cli(scene.root, "generate", "--source", "head").returncode, 0
        )
        scene.commit("locked")
        base = scene.head()
        scene.json_file("a/meta.json", {"version": "1.0.1"})
        self.assertEqual(
            run_cli(scene.root, "generate", "--source", "working-tree").returncode, 0
        )
        scene.commit("bumped")
        argv_by_command["review"] = ("review", "--base", base, "--target", scene.head())
        return argv_by_command

    def test_which_commands_read_a_version_source_is_derived_not_assumed(self):
        """Observe the surface rather than trusting the obligation's list.

        The register names `explain` and a `verify --status` spelling. The
        parser registers no such flag, and `explain` reaches no version source
        at all - it reports a recorded lockfile against current fingerprints,
        and the only reason a broken version source appears in its output is
        config validation running first. The commands are driven in-process so
        the accessor call can be observed; through a subprocess only exit codes
        would be visible.
        """
        observed: List[str] = []
        original = _SourceAccessor.version_read_file

        def counting(accessor, repo_rel):
            observed.append(repo_rel)
            return original(accessor, repo_rel)

        with self._scene() as scene:
            argv_by_command = self._prepare_reader_reachable(scene)
            readers: List[str] = []
            exits: Dict[str, int] = {}
            with mock.patch.object(_SourceAccessor, "version_read_file", counting):
                for command, argv in argv_by_command.items():
                    observed.clear()
                    exits[command] = run_cli_in_process(scene.root, *argv).returncode
                    if observed:
                        readers.append(command)
            self.assertEqual(
                sorted(exits.values()), [0] * len(argv_by_command), f"{exits}"
            )
            self.assertEqual(
                set(readers), {"generate", "verify", "status", "why", "review"}
            )

    # -- a bare ValueError out of the reader, driven through every command --

    def test_no_command_reaches_the_reader_in_the_symlink_scene(self):
        """Why the reader has to be replaced to test the obligation at all.

        A 120000 version-source entry is the closest a real repository comes to
        an unabsorbed reader failure, and not one of the six commands gets far
        enough to raise it: three refuse at config validation, which fails
        closed on the captured mode before any component is measured, and the
        other three refuse earlier still on a missing lockfile or an
        unresolvable base ref. That leaves only the mid-read races - a file
        replaced between the accessor's checks and its read - which cannot be
        produced on demand, so the reader is replaced with one that raises what
        they raise.
        """
        observed: List[str] = []
        original = _SourceAccessor.version_read_file

        def counting(accessor, repo_rel):
            observed.append(repo_rel)
            return original(accessor, repo_rel)

        with self._symlink_entry_scene() as scene:
            with mock.patch.object(_SourceAccessor, "version_read_file", counting):
                for command, argv in VERSION_READING_COMMANDS.items():
                    with self.subTest(command=command):
                        observed.clear()
                        result = run_cli_in_process(scene.root, *argv)
                        self.assertEqual(observed, [])
                        self.assertEqual(result.returncode, 2, result.stdout[:300])
                        self.assertEqual(
                            result.stderr.splitlines()[0],
                            SYMLINK_SCENE_REFUSALS[command],
                        )

    def test_only_the_config_validation_refusals_name_the_symlinked_path(self):
        """The naming is a property of the layer that speaks, and says so.

        `generate`, `verify` and `explain` reach config validation, which names
        `link.json`; `status` and `why` refuse on the missing lockfile and
        `review` on the unresolvable base ref, and neither of those refusals has
        seen the version source at all, so neither can name it.
        """
        validation_refusals = {
            command
            for command, line in SYMLINK_SCENE_REFUSALS.items()
            if line == "ERROR: Config is invalid (1 issues):"
        }
        self.assertEqual(validation_refusals, {"generate", "verify", "explain"})
        with self._symlink_entry_scene() as scene:
            named = {
                command: "link.json" in run_cli(scene.root, *argv).stderr
                for command, argv in VERSION_READING_COMMANDS.items()
            }
        self.assertEqual(
            {command for command, hit in named.items() if hit}, validation_refusals
        )

    def test_the_poisoned_reader_fires_in_every_command_that_reads_one(self):
        """The premise for the two tests below: the replacement is reached.

        Without this, "generate refuses" and "review tracebacks" would be
        claims about a program that might have failed for any other reason.
        Each command is run twice against the same repository, once with the
        real reader and once with the replacement, and the healthy run has to
        succeed for the poisoned outcome to mean anything.
        """
        with self._scene() as scene:
            argv_by_command = self._prepare_reader_reachable(scene)
            for command, argv in argv_by_command.items():
                with self.subTest(command=command):
                    healthy = _run_cli_program(
                        _HEALTHY_READER_PROGRAM, scene.root, *argv
                    )
                    self.assertEqual(healthy.returncode, 0, healthy.stderr[:300])
                    self.assertNotIn(TRACEBACK_MARKER, healthy.stderr)
                    poisoned = _run_cli_program(
                        _POISONED_READER_PROGRAM, scene.root, *argv
                    )
                    if command == "explain":
                        # `explain` reads no version source, so the replacement
                        # is never called and nothing about it may change.
                        self.assertEqual(poisoned.returncode, 0, poisoned.stderr[:300])
                        self.assertEqual(poisoned.stdout, healthy.stdout)
                    else:
                        self.assertIn(
                            "File changed while hashing: a/meta.json",
                            poisoned.stdout + poisoned.stderr,
                        )

    def test_gating_reading_commands_diagnose_a_bare_reader_error_at_exit_two(self):
        """Every gating reader emits one controlled safety failure."""
        with self._scene() as scene:
            argv_by_command = self._prepare_reader_reachable(scene)
            for command in ("generate", "verify", "why", "review"):
                with self.subTest(command=command):
                    result = _run_cli_program(
                        _POISONED_READER_PROGRAM,
                        scene.root,
                        *argv_by_command[command],
                    )
                    self.assertNotIn(TRACEBACK_MARKER, result.stderr)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("a/meta.json", result.stderr)

    def test_the_blast_radius_of_a_bare_reader_error_is_pinned_per_command(self):
        """Pin all six answers, so a partial fix cannot pass unnoticed."""
        with self._scene() as scene:
            argv_by_command = self._prepare_reader_reachable(scene)
            for command, (code, stderr_lines) in POISONED_READER_OUTCOMES.items():
                with self.subTest(command=command):
                    result = _run_cli_program(
                        _POISONED_READER_PROGRAM,
                        scene.root,
                        *argv_by_command[command],
                    )
                    self.assertEqual(result.returncode, code, result.stderr[:300])
                    self.assertEqual(result.stderr.splitlines(), stderr_lines)

    def test_review_converts_a_bare_reader_error_to_a_diagnostic(self):
        with self._scene() as scene:
            argv_by_command = self._prepare_reader_reachable(scene)
            result = _run_cli_program(
                _POISONED_READER_PROGRAM, scene.root, *argv_by_command["review"]
            )
            self.assertEqual(result.returncode, 2, result.stderr[:300])
            self.assertNotIn(TRACEBACK_MARKER, result.stderr)
            self.assertEqual(
                result.stderr.splitlines(),
                ["ERROR: review failed: File changed while hashing: a/meta.json"],
            )

    def test_status_reports_a_bare_reader_error_as_non_gating_drift(self):
        with self._scene() as scene:
            argv_by_command = self._prepare_reader_reachable(scene)
            result = _run_cli_program(
                _POISONED_READER_PROGRAM, scene.root, *argv_by_command["status"]
            )
            self.assertEqual(result.returncode, 0, result.stderr[:300])
            self.assertIn("Project: scenario", result.stdout)
            self.assertIn("DRIFT DETECTED", result.stdout)
            self.assertIn("File changed while hashing: a/meta.json", result.stdout)
            self.assertEqual(result.stderr, "")

    def test_generate_refuses_the_symlinked_version_source_at_config_validation(self):
        """Pin which layer speaks, so a change of layer is visible."""
        with self._symlink_entry_scene() as scene:
            result = run_cli(scene.root, "generate", "--source", "head")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(
                result.stderr.splitlines()[:2],
                [
                    "ERROR: Config is invalid (1 issues):",
                    "  - Component 'a' version_source.file must be a regular file "
                    "in captured head source: 'link.json'",
                ],
            )


if __name__ == "__main__":
    unittest.main()
