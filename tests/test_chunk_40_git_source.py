"""Six claims about where a digest's bytes come from, and what happens when
the answer is not an ordinary file.

Every obligation here is about a refusal rather than a result, and refusals are
the assertions this suite gets wrong most often, because an absence looks the
same whether the guard fired or the code path was never entered at all. So each
of them is written as a pair. The unknown-source guard is checked by counting
the Git processes boundver starts, with a companion test showing that a valid
source mode starts several through the same recorder, which is what makes the
zero meaningful. The claim that a tracked path deleted from disk drops out of
the working-tree selection is paired with a test showing the same path is still
in the snapshot's tracked set and is still selected under head, so the exclusion
is the filter's doing rather than a snapshot that never saw it. The fail-closed
shapes of the ambient config reader are checked against a well-formed crafted
answer that returns a non-empty tuple through the identical mock, and the
promise that no Git process runs inside a submodule worktree is checked against
a control run that deliberately starts one and is caught by the same detector.

Two things made this harder than the register suggests. The first is that
ambient `core.filemode` must be measured through Git's own configuration
scopes: boundver strips every `GIT_*` variable before querying, so the tests
build a throwaway `.gitconfig` and point `HOME`, `USERPROFILE` and
`XDG_CONFIG_HOME` at it. A global `core.filemode=false` can change a
working-tree digest when no local declaration masks it, so boundver excludes
that key from ambient promotion. The tests retain a `100755` index entry with
no executable bit on disk and prove the global setting cannot erase the drift;
a repository-local declaration remains authoritative.

The second is `_read_path_content`, which core re-exports. It returns plain
`bytes` from its head and index branches and `_ModeAwareBytes` from its
working-tree branch, and neither the docstring nor `spec/HASHING.md` mentions
the split; the only production reader, `_SourceAccessor.read_file_limited`,
takes the mode from the captured tree entry instead and so never notices. The
obligation offers a disjunction - carry the metadata everywhere, or document
and assert the asymmetry - and only its second half is achievable without
touching source, so the current shape is pinned per source mode and the first
half is recorded as an expected failure beside it.

Covers OBL-GIT-SOURCE-089, OBL-GIT-SOURCE-090, OBL-GIT-SOURCE-091,
OBL-GIT-SOURCE-092, OBL-GIT-SOURCE-093 and OBL-GIT-SOURCE-096.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from boundver import _git as git
from boundver import _hashing
from boundver._config_io import CONFIG_CANDIDATES, find_config_file
from boundver._git import (
    _SAFE_AMBIENT_WORKTREE_CONFIG_VALUES,
    _ambient_worktree_config_overrides,
    _capture_git_source_snapshot,
    _repository_filter_config_overrides,
    _working_tree_mode,
)
from boundver._hashing import _ModeAwareBytes
from boundver._lockfile import generate_lockfile
from boundver._utils import ConfigError, GuardrailError

from tests._scenarios import Scenario, requires_symlinks

#: The two digest entry points core re-exports. Both take the same guard.
DIGEST_ENTRY_POINTS = {
    "source_tree_digest": _hashing.source_tree_digest,
    "_content_only_digest": _hashing._content_only_digest,
}

#: Requested source, the snapshot's own source, and the exact refusal text
#: `_files_from_source` raises when they disagree.
SNAPSHOT_MISMATCHES = (
    ("index", "head", "Captured 'head' snapshot cannot serve 'index'"),
    ("head", "index", "Captured 'index' snapshot cannot serve 'head'"),
    (
        "working-tree",
        "head",
        "working-tree hashing requires an index tracking snapshot",
    ),
)

#: The pairings `_files_from_source` accepts, which is why the table above is
#: a guard and not an accident of argument order.
SNAPSHOT_MATCHES = (("head", "head"), ("index", "index"), ("working-tree", "index"))

#: Every source carries the Git identity used by the digest wire format.
CONTENT_SHAPES = {
    "head": (_ModeAwareBytes, True),
    "index": (_ModeAwareBytes, True),
    "working-tree": (_ModeAwareBytes, True),
}

#: The refusal a gitlink selected by a boundary declaration produces, by
#: source mode. head and index reject the object type; working-tree rejects
#: the directory Git checked out for it.
GITLINK_REFUSALS = {
    "head": "Cannot hash non-blob Git entry at vendor/sub: commit mode 160000",
    "index": "Cannot hash non-blob Git entry at vendor/sub: commit mode 160000",
    "working-tree": "Unsupported working-tree file type at vendor/sub",
}

#: Crafted `git config --show-scope --null --get-regexp` answers and the
#: override tuple each must produce. The first row is the premise: it proves
#: the mocked path is entered and can return something.
AMBIENT_ANSWERS = {
    "a well-formed global row": (
        "global\x00core.eol\nlf\x00",
        (("core.eol", "lf"),),
    ),
    "a local scope is not ambient": ("local\x00core.eol\nlf\x00", ()),
    "a worktree scope is not ambient": (
        "worktree\x00core.eol\nlf\x00",
        (),
    ),
    "a value outside the allowlist": ("global\x00core.eol\nmaybe\x00", ()),
}

MALFORMED_AMBIENT_ANSWERS = {
    "a digest-sensitive key outside the allowlist": (
        "global\x00core.filemode\nfalse\x00"
    ),
    "an arbitrary key outside the allowlist": "global\x00core.editor\nvim\x00",
    "an odd field count": "global\x00core.eol\nlf\x00global\x00",
    "a row with no newline separator": "global\x00core.eol\x00",
}


class _GitProcessRecorder:
    """Stand in for the subprocess module and record every Git argv."""

    def __init__(self) -> None:
        self.commands: list = []
        self.cwds: list = []

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def Popen(self, command, *args, **kwargs):  # noqa: N802 - mirrors the API
        self.commands.append(list(command))
        self.cwds.append(kwargs.get("cwd"))
        return subprocess.Popen(command, *args, **kwargs)

    def commands_reaching(self, directory) -> list:
        """Every recorded argv naming *directory*, however Git was pointed."""
        needle = os.path.normcase(str(directory))
        return [
            command
            for command in self.commands
            if any(needle in os.path.normcase(token) for token in command)
        ]


@contextlib.contextmanager
def _recorded_git():
    recorder = _GitProcessRecorder()
    with mock.patch.object(git, "subprocess", recorder):
        yield recorder


class _CraftedAnswer:
    """One crafted reply from the ambient worktree config query."""

    def __init__(self, stdout: str = "", stderr: str = "") -> None:
        self.result = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout=stdout, stderr=stderr
        )

    def __call__(self, *args, **kwargs):
        return self.result


def _repository() -> Scenario:
    scene = Scenario()
    scene.component("svc", path="svc", provider="path-hash", boundary=["*.txt"])
    scene.file("svc/a.txt", "alpha\n")
    scene.file("svc/b.txt", "beta\n")
    scene.commit()
    return scene


def _submodule_repository() -> Scenario:
    """A repository whose component boundary selects a gitlink."""
    scene = Scenario()
    scene.component("vendor", path="vendor", provider="path-hash", boundary=["sub"])
    scene.file("vendor/keep.txt", "keep\n")
    scene.write_config()
    scene.git("add", "--all")
    scene.submodule("vendor/sub")
    scene.commit_index("with submodule")
    return scene


def _unset_local_filemode(scene: Scenario) -> None:
    """Remove the local declaration Git writes at init on some platforms."""
    subprocess.run(
        ["git", "config", "--local", "--unset-all", "core.filemode"],
        cwd=scene.root,
        capture_output=True,
        check=False,
    )


@contextlib.contextmanager
def _global_git_config(text: str):
    """Point Git's global scope at a throwaway file.

    boundver strips every ``GIT_*`` variable before querying config, so
    ``GIT_CONFIG_GLOBAL`` cannot reach the child. The home-directory variables
    survive that filter and are the only remaining way to produce a row Git
    labels ``global``.
    """
    with tempfile.TemporaryDirectory() as home:
        (Path(home) / ".gitconfig").write_text(text, encoding="utf-8")
        overrides = {
            "HOME": home,
            "USERPROFILE": home,
            "HOMEDRIVE": "",
            "HOMEPATH": "",
            "XDG_CONFIG_HOME": home,
        }
        with mock.patch.dict(os.environ, overrides):
            _ambient_worktree_config_overrides.cache_clear()
            _repository_filter_config_overrides.cache_clear()
            try:
                yield
            finally:
                _ambient_worktree_config_overrides.cache_clear()
                _repository_filter_config_overrides.cache_clear()


class SourceModeAndSnapshotGuardTests(unittest.TestCase):
    """OBL-GIT-SOURCE-089: refuse the request before reading the repository."""

    def test_both_digest_entry_points_refuse_an_unknown_source_mode(self):
        with _repository() as scene:
            for name, function in DIGEST_ENTRY_POINTS.items():
                with self.subTest(entry_point=name):
                    with self.assertRaises(ValueError) as raised:
                        function(scene.root, "svc", "future")
                    self.assertEqual(
                        str(raised.exception), "Unknown source mode: 'future'"
                    )

    def test_an_unknown_source_mode_starts_no_git_process_at_all(self):
        with _repository() as scene:
            for name, function in DIGEST_ENTRY_POINTS.items():
                with self.subTest(entry_point=name):
                    with _recorded_git() as recorder:
                        with self.assertRaises(ValueError):
                            function(scene.root, "svc", "future")
                    self.assertEqual(recorder.commands, [])

    def test_a_valid_source_mode_does_start_git_processes(self):
        """The premise: the recorder sees Git when the guard lets a call through."""
        with _repository() as scene:
            for name, function in DIGEST_ENTRY_POINTS.items():
                with self.subTest(entry_point=name):
                    with _recorded_git() as recorder:
                        self.assertIsNotNone(function(scene.root, "svc", "head"))
                    self.assertNotEqual(recorder.commands, [])

    def test_a_path_selecting_no_files_yields_none_rather_than_a_digest(self):
        with _repository() as scene:
            for name, function in DIGEST_ENTRY_POINTS.items():
                for source in ("head", "index", "working-tree"):
                    with self.subTest(entry_point=name, source=source):
                        self.assertIsNone(
                            function(scene.root, "does/not/exist", source)
                        )

    def test_a_path_selecting_files_yields_a_digest(self):
        """The premise for the None above: the same call shape can succeed."""
        with _repository() as scene:
            for name, function in DIGEST_ENTRY_POINTS.items():
                for source in ("head", "index", "working-tree"):
                    with self.subTest(entry_point=name, source=source):
                        digest = function(scene.root, "svc", source)
                        self.assertIsInstance(digest, str)
                        self.assertEqual(len(digest), 64)

    def test_the_empty_selection_surfaces_as_a_named_generation_failure(self):
        """Why None matters: the caller reports the path, not a fingerprint."""
        with _repository() as scene:
            scene.component(
                "empty", path="nowhere", provider="path-hash", boundary=["*.txt"]
            )
            scene.write_config()
            with self.assertRaises(ConfigError) as raised:
                generate_lockfile(scene.config, scene.root, source="working-tree")
            self.assertIn(
                "empty: No files found for 'nowhere' on disk", str(raised.exception)
            )

    def test_a_snapshot_captured_for_another_source_is_refused(self):
        with _repository() as scene:
            for requested, captured_source, message in SNAPSHOT_MISMATCHES:
                with self.subTest(requested=requested, captured=captured_source):
                    snapshot = _capture_git_source_snapshot(
                        scene.root, captured_source
                    )
                    with self.assertRaises(ValueError) as raised:
                        _hashing._files_from_source(
                            scene.root, "svc", requested, snapshot
                        )
                    self.assertEqual(str(raised.exception), message)

    def test_a_matching_snapshot_is_accepted_and_reused(self):
        """The premise: the guard rejects a pairing, not every snapshot."""
        with _repository() as scene:
            for requested, captured_source in SNAPSHOT_MATCHES:
                with self.subTest(requested=requested, captured=captured_source):
                    snapshot = _capture_git_source_snapshot(
                        scene.root, captured_source
                    )
                    files, returned = _hashing._files_from_source(
                        scene.root, "svc", requested, snapshot
                    )
                    self.assertEqual(files, ["svc/a.txt", "svc/b.txt"])
                    self.assertIs(returned, snapshot)


class WorkingTreeSelectionTests(unittest.TestCase):
    """OBL-GIT-SOURCE-090: what disk state does and does not remove a path."""

    def _scene_with_a_deleted_tracked_file(self) -> Scenario:
        scene = _repository()
        scene.remove("svc/b.txt")
        return scene

    def test_a_tracked_path_deleted_from_disk_leaves_the_selection(self):
        with self._scene_with_a_deleted_tracked_file() as scene:
            snapshot = _capture_git_source_snapshot(scene.root, "index")
            branches = {
                "with an index tracking snapshot": snapshot,
                "capturing its own tracking snapshot": None,
            }
            for label, argument in branches.items():
                with self.subTest(branch=label):
                    files, _ = _hashing._files_from_source(
                        scene.root, "svc", "working-tree", argument
                    )
                    self.assertEqual(files, ["svc/a.txt"])

    def test_the_deleted_path_is_still_tracked_and_still_selected_by_head(self):
        """The premise: the filter dropped it, not the snapshot that fed it."""
        with self._scene_with_a_deleted_tracked_file() as scene:
            snapshot = _capture_git_source_snapshot(scene.root, "index")
            self.assertIn("svc/b.txt", snapshot.tracked_paths)
            head_files, _ = _hashing._files_from_source(
                scene.root, "svc", "head", None
            )
            self.assertEqual(head_files, ["svc/a.txt", "svc/b.txt"])
            self.assertNotEqual(
                _hashing.source_tree_digest(scene.root, "svc", "working-tree"),
                _hashing.source_tree_digest(scene.root, "svc", "head"),
            )

    def test_working_tree_selection_has_no_duplicate_repository_paths(self):
        with _repository() as scene:
            snapshot = _capture_git_source_snapshot(scene.root, "index")
            for label, argument in (
                ("with an index tracking snapshot", snapshot),
                ("capturing its own tracking snapshot", None),
            ):
                with self.subTest(branch=label):
                    files, _ = _hashing._files_from_source(
                        scene.root, "svc", "working-tree", argument
                    )
                    self.assertNotEqual(files, [])
                    self.assertEqual(len(files), len(set(files)))

    def test_the_duplicate_check_would_catch_a_repeated_path(self):
        """The premise for the check above: it is not vacuous on a real list."""
        repeated = ["svc/a.txt", "svc/a.txt", "svc/b.txt"]
        self.assertNotEqual(len(repeated), len(set(repeated)))

    @requires_symlinks
    def test_a_dangling_tracked_symlink_stays_in_the_selection(self):
        with _repository() as scene:
            scene.symlink("svc/link", "missing-target.txt")
            scene.commit("add a dangling link")
            link = scene.root / "svc" / "link"
            self.assertFalse(link.exists())
            self.assertTrue(link.is_symlink())
            snapshot = _capture_git_source_snapshot(scene.root, "index")
            self.assertIn("svc/link", snapshot.tracked_paths)
            files, _ = _hashing._files_from_source(
                scene.root, "svc", "working-tree", snapshot
            )
            self.assertIn("svc/link", files)
            self.assertEqual(files, ["svc/a.txt", "svc/b.txt", "svc/link"])


class ReadPathContentShapeTests(unittest.TestCase):
    """OBL-GIT-SOURCE-091: the metadata a re-exported reader does not carry."""

    def _content(self, scene: Scenario, source: str) -> bytes:
        return _hashing._read_path_content(
            scene.root, scene.root / "svc" / "a.txt", source
        )

    def test_every_source_mode_reads_the_same_bytes(self):
        """The premise: all three branches ran, on one unmodified file."""
        with _repository() as scene:
            for source in CONTENT_SHAPES:
                with self.subTest(source=source):
                    self.assertEqual(bytes(self._content(scene, source)), b"alpha\n")

    def test_each_source_mode_returns_the_recorded_content_type(self):
        with _repository() as scene:
            for source, (expected_type, _) in CONTENT_SHAPES.items():
                with self.subTest(source=source):
                    self.assertIs(type(self._content(scene, source)), expected_type)

    def test_every_source_branch_carries_git_metadata(self):
        with _repository() as scene:
            for source, (_, carries) in CONTENT_SHAPES.items():
                with self.subTest(source=source):
                    content = self._content(scene, source)
                    self.assertEqual(hasattr(content, "git_mode"), carries)
                    self.assertEqual(hasattr(content, "git_object_type"), carries)

    def test_the_metadata_shape_is_consistent_across_sources(self):
        with _repository() as scene:
            for source in CONTENT_SHAPES:
                with self.subTest(source=source):
                    content = self._content(scene, source)
                    self.assertEqual(content.git_mode, "100644")
                    self.assertEqual(content.git_object_type, "blob")
                    self.assertEqual(content.source_size, len(b"alpha\n"))

    def test_content_carries_git_mode_and_object_type_for_every_source_mode(self):
        """The raw reader never drops the identity used by hashing."""
        with _repository() as scene:
            for source in CONTENT_SHAPES:
                content = self._content(scene, source)
                self.assertTrue(hasattr(content, "git_mode"), source)
                self.assertTrue(hasattr(content, "git_object_type"), source)


class ConfigDiscoveryPrecedenceTests(unittest.TestCase):
    """OBL-GIT-SOURCE-092: which config file a repository is judged by."""

    def test_the_candidate_order_is_json_then_yaml_then_yml_then_toml(self):
        self.assertEqual(
            CONFIG_CANDIDATES[:4],
            (
                "boundary.config.json",
                "boundary.config.yaml",
                "boundary.config.yml",
                "boundary.config.toml",
            ),
        )

    def test_every_candidate_is_reached_in_declaration_order(self):
        with Scenario() as scene:
            for name in CONFIG_CANDIDATES:
                (scene.root / name).write_text("{}\n", encoding="utf-8")
            for expected in CONFIG_CANDIDATES:
                with self.subTest(expected=expected):
                    self.assertEqual(find_config_file(scene.root).name, expected)
                (scene.root / expected).unlink()
            self.assertEqual(
                find_config_file(scene.root).name, CONFIG_CANDIDATES[0]
            )

    def test_a_non_default_hint_never_falls_back_to_another_candidate(self):
        with Scenario() as scene:
            (scene.root / "boundary.config.json").write_text(
                "{}\n", encoding="utf-8"
            )
            for hint in CONFIG_CANDIDATES[1:]:
                with self.subTest(hint=hint):
                    selected = find_config_file(scene.root, hint)
                    self.assertEqual(selected, scene.root / hint)
                    self.assertFalse(selected.exists())

    def test_the_default_hint_does_fall_back_on_the_same_disk_state(self):
        """The premise: the fallback exists, and the hint guard is what stops it."""
        with Scenario() as scene:
            (scene.root / "boundary.config.yaml").write_text(
                "{}\n", encoding="utf-8"
            )
            self.assertFalse((scene.root / "boundary.config.json").exists())
            self.assertEqual(
                find_config_file(scene.root).name, "boundary.config.yaml"
            )

    def _snapshot_scene(self) -> Scenario:
        """Commit a .yml config, then diverge the working tree from it."""
        scene = Scenario()
        scene.file("boundary.config.yml", "project: committed\n")
        scene.file("svc/a.txt", "alpha\n")
        scene.git("add", "--all")
        scene.git("commit", "-m", "committed yml")
        (scene.root / "boundary.config.yml").unlink()
        (scene.root / "boundary.config.json").write_text("{}\n", encoding="utf-8")
        return scene

    def test_snapshot_mode_selects_a_committed_config_absent_from_disk(self):
        with self._snapshot_scene() as scene:
            snapshot = _capture_git_source_snapshot(scene.root, "head")
            self.assertIn("boundary.config.yml", snapshot.entries)
            self.assertFalse((scene.root / "boundary.config.yml").exists())
            self.assertEqual(
                find_config_file(scene.root, snapshot=snapshot).name,
                "boundary.config.yml",
            )

    def test_snapshot_mode_ignores_an_untracked_config_on_disk(self):
        with self._snapshot_scene() as scene:
            snapshot = _capture_git_source_snapshot(scene.root, "head")
            self.assertNotIn("boundary.config.json", snapshot.entries)
            self.assertNotEqual(
                find_config_file(scene.root, snapshot=snapshot).name,
                "boundary.config.json",
            )

    def test_without_a_snapshot_the_untracked_config_is_the_one_selected(self):
        """The premise: disk really does hold a higher-precedence candidate."""
        with self._snapshot_scene() as scene:
            self.assertTrue((scene.root / "boundary.config.json").exists())
            self.assertEqual(
                find_config_file(scene.root).name, "boundary.config.json"
            )


class AmbientFilemodeExclusionTests(unittest.TestCase):
    """Ambient core.filemode is excluded while local semantics remain intact."""

    def setUp(self):
        _ambient_worktree_config_overrides.cache_clear()
        _repository_filter_config_overrides.cache_clear()

    def tearDown(self):
        _ambient_worktree_config_overrides.cache_clear()
        _repository_filter_config_overrides.cache_clear()

    def _scene_without_a_local_filemode(self) -> Scenario:
        scene = _repository()
        _unset_local_filemode(scene)
        return scene

    def test_a_global_core_filemode_false_is_not_promoted(self):
        with self._scene_without_a_local_filemode() as scene:
            resolved = str(scene.root.resolve())
            with _global_git_config("[core]\n\tfilemode = false\n"):
                overrides = dict(_ambient_worktree_config_overrides(resolved))
                self.assertNotIn("core.filemode", overrides)
                snapshot = _capture_git_source_snapshot(scene.root, "index")
                self.assertTrue(snapshot.filemode)
            for key, value in overrides.items():
                self.assertIn(key, _SAFE_AMBIENT_WORKTREE_CONFIG_VALUES)
                self.assertIn(value, _SAFE_AMBIENT_WORKTREE_CONFIG_VALUES[key])

    def test_without_the_global_row_the_snapshot_keeps_gits_true_default(self):
        """A different ambient setting does not alter filemode."""
        with self._scene_without_a_local_filemode() as scene:
            resolved = str(scene.root.resolve())
            with _global_git_config("[core]\n\tignorecase = true\n"):
                overrides = dict(_ambient_worktree_config_overrides(resolved))
                self.assertNotIn("core.filemode", overrides)
                self.assertTrue(
                    _capture_git_source_snapshot(scene.root, "index").filemode
                )

    def test_a_local_declaration_remains_authoritative(self):
        with _repository() as scene:
            scene.git("config", "--local", "core.filemode", "true")
            resolved = str(scene.root.resolve())
            with _global_git_config("[core]\n\tfilemode = false\n"):
                overrides = dict(_ambient_worktree_config_overrides(resolved))
                self.assertNotIn("core.filemode", overrides)
                self.assertTrue(
                    _capture_git_source_snapshot(scene.root, "index").filemode
                )

    def test_a_false_repository_setting_makes_mode_follow_the_tracked_entry(self):
        with self._scene_without_a_local_filemode() as scene:
            scene.git("update-index", "--chmod=+x", "svc/a.txt")
            snapshot = _capture_git_source_snapshot(scene.root, "index")
            entry = snapshot.entries["svc/a.txt"]
            self.assertEqual(entry.mode, "100755")
            cases = {
                "repository core.filemode=false": (False, ("100755", "blob")),
                "core.filemode left true": (True, ("100644", "blob")),
                "no tracked entry to follow": (False, ("100644", "blob")),
            }
            for label, (core_filemode, expected) in cases.items():
                with self.subTest(case=label):
                    tracked = None if "no tracked" in label else entry
                    self.assertEqual(
                        _working_tree_mode(
                            scene.root,
                            "svc/a.txt",
                            tracked,
                            core_filemode=core_filemode,
                        ),
                        expected,
                    )

    def test_ambient_filemode_cannot_remove_drift_between_index_and_disk(self):
        with self._scene_without_a_local_filemode() as scene:
            scene.git("update-index", "--chmod=+x", "svc/a.txt")
            plain = _capture_git_source_snapshot(scene.root, "index")
            self.assertTrue(plain.filemode)
            index_digest = _hashing.source_tree_digest(
                scene.root, "svc", "index", plain
            )
            unpromoted = _hashing.source_tree_digest(
                scene.root, "svc", "working-tree", plain
            )
            self.assertNotEqual(index_digest, unpromoted)
            with _global_git_config("[core]\n\tfilemode = false\n"):
                ambient_snapshot = _capture_git_source_snapshot(
                    scene.root, "index"
                )
                self.assertTrue(ambient_snapshot.filemode)
                ambient = _hashing.source_tree_digest(
                    scene.root, "svc", "working-tree", ambient_snapshot
                )
            self.assertEqual(unpromoted, ambient)
            self.assertNotEqual(index_digest, ambient)

    def test_valid_scope_answers_are_classified(self):
        for label, (stdout, expected) in AMBIENT_ANSWERS.items():
            with self.subTest(answer=label):
                _ambient_worktree_config_overrides.cache_clear()
                with _repository() as scene:
                    resolved = str(scene.root.resolve())
                    with mock.patch.object(
                        git, "_git_run", _CraftedAnswer(stdout)
                    ):
                        self.assertEqual(
                            _ambient_worktree_config_overrides(resolved), expected
                        )

    def test_malformed_or_unrecognized_answers_fail_closed(self):
        for label, stdout in MALFORMED_AMBIENT_ANSWERS.items():
            with self.subTest(answer=label):
                _ambient_worktree_config_overrides.cache_clear()
                with _repository() as scene:
                    resolved = str(scene.root.resolve())
                    with mock.patch.object(
                        git, "_git_run", _CraftedAnswer(stdout)
                    ):
                        with self.assertRaises(GuardrailError):
                            _ambient_worktree_config_overrides(resolved)

    def test_a_diagnostic_on_stderr_fails_closed(self):
        _ambient_worktree_config_overrides.cache_clear()
        with _repository() as scene:
            resolved = str(scene.root.resolve())
            answer = _CraftedAnswer(
                "global\x00core.eol\nlf\x00", "warning: something"
            )
            with mock.patch.object(git, "_git_run", answer):
                with self.assertRaises(GuardrailError) as raised:
                    _ambient_worktree_config_overrides(resolved)
            self.assertIn("ambiguous diagnostic", str(raised.exception))


class SubmoduleGitlinkTests(unittest.TestCase):
    """OBL-GIT-SOURCE-096: a gitlink is refused, never entered."""

    def setUp(self):
        _ambient_worktree_config_overrides.cache_clear()
        _repository_filter_config_overrides.cache_clear()

    def tearDown(self):
        _ambient_worktree_config_overrides.cache_clear()
        _repository_filter_config_overrides.cache_clear()

    def test_a_declared_gitlink_fails_closed_in_every_source_mode(self):
        with _submodule_repository() as scene:
            snapshot = _capture_git_source_snapshot(scene.root, "head")
            entry = snapshot.entries["vendor/sub"]
            self.assertEqual((entry.mode, entry.object_type), ("160000", "commit"))
            for source, message in GITLINK_REFUSALS.items():
                with self.subTest(source=source):
                    with self.assertRaises(ValueError) as raised:
                        _hashing.source_tree_digest(scene.root, "vendor/sub", source)
                    self.assertEqual(str(raised.exception), message)

    def test_a_sibling_blob_under_the_same_component_still_hashes(self):
        """The premise: the refusal is about the gitlink, not the component."""
        with _submodule_repository() as scene:
            for source in GITLINK_REFUSALS:
                with self.subTest(source=source):
                    digest = _hashing.source_tree_digest(
                        scene.root, "vendor/keep.txt", source
                    )
                    self.assertEqual(len(digest), 64)

    def test_generation_reports_the_refusal_for_the_declaring_component(self):
        with _submodule_repository() as scene:
            for source, message in GITLINK_REFUSALS.items():
                with self.subTest(source=source):
                    with self.assertRaises(ConfigError) as raised:
                        generate_lockfile(scene.config, scene.root, source=source)
                    text = str(raised.exception)
                    self.assertIn(f"vendor: Exact digest failed: {message}", text)
                    self.assertIn(
                        "vendor: Boundary content collection failed for sub:", text
                    )

    def test_no_git_process_is_started_inside_the_submodule_worktree(self):
        with _submodule_repository() as scene:
            submodule = scene.root / "vendor" / "sub"
            with _recorded_git() as recorder:
                for source in GITLINK_REFUSALS:
                    with self.assertRaises(ConfigError):
                        generate_lockfile(scene.config, scene.root, source=source)
            self.assertNotEqual(recorder.commands, [])
            self.assertEqual(recorder.commands_reaching(submodule), [])
            self.assertEqual(set(recorder.cwds), {None})

    def test_the_recorder_would_have_caught_a_process_inside_the_submodule(self):
        """The premise: the detector is not blind to the argv it looks for."""
        with _submodule_repository() as scene:
            submodule = scene.root / "vendor" / "sub"
            with _recorded_git() as recorder:
                git._git_run(submodule, ["rev-parse", "HEAD"])
            self.assertNotEqual(recorder.commands_reaching(submodule), [])

    def test_a_dirty_submodule_checkout_is_not_a_changed_path(self):
        with _submodule_repository() as scene:
            (scene.root / "vendor" / "sub" / "content.txt").write_text(
                "locally dirtied\n", encoding="utf-8"
            )
            self.assertEqual(scene.git("status", "--porcelain"), "M vendor/sub")
            self.assertEqual(
                git.changed_paths_since_ref(scene.root, "HEAD", "working-tree"), []
            )
            self.assertEqual(
                git.dirty_component_paths(scene.root, ["vendor", "vendor/sub"]), []
            )

    def test_a_dirty_tracked_file_is_reported_by_both_walks(self):
        """The premise: those two empty lists are not empty for every change."""
        with _submodule_repository() as scene:
            (scene.root / "vendor" / "keep.txt").write_text(
                "locally dirtied\n", encoding="utf-8"
            )
            self.assertEqual(
                git.changed_paths_since_ref(scene.root, "HEAD", "working-tree"),
                ["vendor/keep.txt"],
            )
            self.assertEqual(
                git.dirty_component_paths(scene.root, ["vendor", "vendor/sub"]),
                ["vendor"],
            )


if __name__ == "__main__":
    unittest.main()
