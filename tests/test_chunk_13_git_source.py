"""Symlinks that never appear on disk, and three commands that write config files.

Six obligations, two subjects. The first three are about symlinks, and the host
that has to run them cannot create one: Windows refuses ``symlink()`` with
WinError 1314 unless the process holds SeCreateSymbolicLinkPrivilege, so
``tests._scenarios.supports_symlinks`` is False here and every test written
against a real link would silently skip. Skipping is the wrong answer for
OBL-GIT-SOURCE-094 and OBL-GIT-SOURCE-095, because the code they name reads a
symlink through exactly two narrow interfaces - a mode-120000 Git tree entry,
and a ``lstat``/``readlink``/``exists``/``is_symlink`` quartet on one path - and
both interfaces can be presented honestly without a filesystem that supports
links. A mode-120000 entry is made with ``git update-index --cacheinfo``, which
is a real committed symlink as far as every reader in ``_git`` is concerned;
the working-tree view of a dangling link is posed at the ``pathlib``/``os``
boundary by ``_PosedSymlink``, which answers for one absolute path and
delegates for every other. So OBL-GIT-SOURCE-094 needs no pose at all - its
whole point is a tree entry compared against a regular file - while
OBL-GIT-SOURCE-031 and OBL-GIT-SOURCE-095 are exercised against posed link
metadata, and the residual gap is named rather than hidden: no test here proves
that a POSIX kernel reports what the pose reports.

The pose earns its keep because each of these obligations asserts an absence,
and each absence has a premise that fires without it. A tracked path missing
from disk really is dropped from ``_list_files_for_source`` and really is
reported ``("D", path)``; both are asserted before the dangling-link tests
claim the opposite, so the ``is_symlink()`` disjunct at _git.py:2515-2519 has
somewhere to show up. The pre-read size guard is asserted not to call
``readlink`` only after a test has watched ``readlink`` be called and recorded.
And the second size check in ``_read_path_content`` - the one the register said
was unreachable - is reached here by giving the pose an ``st_size`` of 3 while
``readlink`` answers 80 bytes, which is what a filesystem does when a link's
recorded size disagrees with its target. The guardrail names 80, not 3, so the
two checks are told apart by the number in the message rather than by faith.

The other three obligations are about ``init``, ``add`` and ``remove``, and all
three findings are divergences the register asked to have pinned rather than
fixed. ``add`` and ``remove`` call ``validate_config(config, repo_root)``
positionally, which is ``require_slice_facets=False``; a config whose slice asks
for a ``behavior`` digest from a component with no ``behavior.paths`` is
therefore rewritten by ``add`` with a cheerful "Run: boundver generate", and
that generate exits 2 on a defect ``add`` had just declared absent. A Hypothesis
property drives that divergence over a catalogue of declarations chosen so the
facet each can supply is known from the spec rule rather than from the code, and
confirms the weak verdict is always a subset of the strict one and that the two
differ exactly when a slice member cannot supply its mode. ``--config`` with an
absolute path outside the repository is read, validated against the *current*
repository and rewritten in place, which is pinned in both directions: a path
that exists only in the repository is accepted, and one that exists only beside
the config is refused. Finally, OBL-GIT-SOURCE-145 names a diagnostic that no
command emits - the refusal for a pre-existing non-regular config path comes
from ``_prepare_atomic_output``, one layer earlier than the ``_write_text_atomic``
checks the obligation cites, and says "Output path must be a regular file, not a
symlink, junction, or reparse point". That mismatch is recorded as an
expected failure with the observed strings pinned beside it.

Covers OBL-GIT-SOURCE-031, OBL-GIT-SOURCE-094, OBL-GIT-SOURCE-095,
OBL-GIT-SOURCE-143, OBL-GIT-SOURCE-144 and OBL-GIT-SOURCE-145.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from unittest import mock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from boundver import core
from boundver._config import validate_config
from boundver._git import (
    _list_files_for_source,
    _working_tree_name_status,
    changed_paths_since_ref,
)
from boundver._hashing import _read_path_content
from boundver._utils import GuardrailError

from tests._parity import run_cli, run_cli_in_process
from tests._scenarios import Scenario

_REAL_LSTAT = Path.lstat
_REAL_EXISTS = Path.exists
_REAL_IS_SYMLINK = Path.is_symlink
_REAL_READLINK = os.readlink

#: Content of the regular file that sits at the path posed as a symlink. If the
#: reader ever dereferences, these bytes are what it would return, so they must
#: not resemble a link target.
PAYLOAD = "the real payload that must never be hashed\n"

#: The link-target text used throughout. No trailing newline: that is exactly
#: what Git stores in a symlink blob, and it is what makes a regular file
#: holding the same text produce an identical content digest.
LINK_TARGET = "real.txt"

#: Component declarations that differ only in which facets they can supply.
#: The facet rule is spelled out again in `declared_facets` below, from
#: spec-level wording rather than from `_available_component_facets`, so the
#: property has an oracle that does not consult the code it is judging.
FACET_CATALOGUE: Dict[str, Dict[str, Any]] = {
    "full": {
        "path": "full",
        "version_source": {"git_tag_prefix": "full-v"},
        "boundary": {"provider": "path-hash", "paths": ["*.txt"]},
        "behavior": {"paths": ["*.txt"]},
    },
    "boundary_only": {
        "path": "boundary_only",
        "version_source": None,
        "boundary": {"provider": "path-hash", "paths": ["*.txt"]},
    },
    "behavior_only": {
        "path": "behavior_only",
        "version_source": None,
        "boundary": {"provider": "leaf", "paths": []},
        "behavior": {"paths": ["*.txt"]},
    },
    "compat_only": {
        "path": "compat_only",
        "version_source": {"git_tag_prefix": "compat-v"},
        "boundary": {"provider": "implicit", "paths": []},
    },
    "bare": {
        "path": "bare",
        "version_source": None,
        "boundary": {"provider": "implicit", "paths": []},
    },
}

#: `supported_modes` in _config.py is `FACET_SET`, so these four are every mode
#: a slice can name. "exact" is the one mode `require_slice_facets` never
#: checks, which is why it belongs in the sample rather than outside it.
SLICE_MODES = ("exact", "boundary", "behavior", "compat")

PROFILE = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow],
)


def declared_facets(component: Dict[str, Any]) -> Set[str]:
    """Which facets a declaration can intentionally produce.

    Restated from the declaration rules rather than imported, so a change to
    `_available_component_facets` cannot move the oracle and the property at
    the same time.
    """
    facets = {"exact"}
    boundary = component.get("boundary")
    if isinstance(boundary, dict):
        provider = boundary.get("provider")
        paths = boundary.get("paths", [])
        if provider not in {"leaf", "implicit"} or (
            provider == "implicit" and isinstance(paths, list) and bool(paths)
        ):
            facets.add("boundary")
    behavior = component.get("behavior")
    if isinstance(behavior, dict) and isinstance(behavior.get("paths"), list):
        if behavior["paths"]:
            facets.add("behavior")
    if isinstance(component.get("version_source"), dict):
        facets.add("compat")
    return facets


class _PosedSymlink(contextlib.AbstractContextManager):
    """Make one absolute path answer as a POSIX symlink, and record readlink.

    Four calls decide what boundver believes about a working-tree path:
    ``Path.lstat`` (does it exist, and is it a link), ``Path.exists`` (does the
    *target* exist), ``Path.is_symlink``, and ``os.readlink``. Each is answered
    here for one path only and delegated for every other, so directory ancestor
    walks, config discovery and Git subprocesses all see the real filesystem.

    ``dangling`` flips ``exists()`` to False while leaving ``lstat`` succeeding,
    which is precisely a link whose target was deleted.
    """

    def __init__(
        self,
        link: Path,
        target: str,
        *,
        st_size: Optional[int] = None,
        dangling: bool = False,
    ) -> None:
        self._key = os.path.normcase(os.fspath(link))
        self._target = target
        self._dangling = dangling
        self._sample = os.stat_result(
            (
                stat.S_IFLNK | 0o777,
                9,
                13,
                1,
                0,
                0,
                len(os.fsencode(target)) if st_size is None else st_size,
                1000,
                1000,
                1000,
            )
        )
        self._stack = contextlib.ExitStack()
        self.readlink_calls: List[str] = []

    def _same(self, path: Any) -> bool:
        return os.path.normcase(os.fspath(path)) == self._key

    def __enter__(self) -> "_PosedSymlink":
        posed = self

        def fake_lstat(self, *args, **kwargs):
            if posed._same(self):
                return posed._sample
            return _REAL_LSTAT(self, *args, **kwargs)

        def fake_exists(self, **kwargs):
            if posed._same(self):
                return not posed._dangling
            return _REAL_EXISTS(self, **kwargs)

        def fake_is_symlink(self):
            if posed._same(self):
                return True
            return _REAL_IS_SYMLINK(self)

        def fake_readlink(path, *args, **kwargs):
            if posed._same(path):
                posed.readlink_calls.append(os.fspath(path))
                return posed._target
            return _REAL_READLINK(path, *args, **kwargs)

        self._stack.enter_context(mock.patch.object(Path, "lstat", fake_lstat))
        self._stack.enter_context(mock.patch.object(Path, "exists", fake_exists))
        self._stack.enter_context(
            mock.patch.object(Path, "is_symlink", fake_is_symlink)
        )
        self._stack.enter_context(mock.patch.object(os, "readlink", fake_readlink))
        return self

    def __exit__(self, *exc: object) -> None:
        self._stack.close()


def _commit_symlink_entry(scene: Scenario, path: str, blob_path: str) -> str:
    """Commit a mode-120000 entry at *path* carrying the blob of *blob_path*.

    ``update-index --cacheinfo`` is how a symlink reaches a tree without a
    filesystem that supports links, and `commit_index` is required because
    `Scenario.commit` runs ``git add --all``, which would drop an index entry
    whose path is absent from disk.
    """
    oid = scene.git("rev-parse", f"HEAD:{blob_path}")
    scene.git("update-index", "--add", "--cacheinfo", f"120000,{oid},{path}")
    scene.commit_index("symlink entry")
    return oid


class SymlinkContentHashingTests(unittest.TestCase):
    """OBL-GIT-SOURCE-031: link text is the content, and both size gates bite."""

    def setUp(self):
        self.scene = Scenario()
        self.addCleanup(self.scene.close)
        self.scene.component(
            "svc", path="svc", provider="path-hash", boundary=["*.txt"]
        )
        self.scene.file("svc/real.txt", PAYLOAD)
        # A real regular file sits where the pose will claim a link is, so a
        # dereference has something distinctive to return.
        self.scene.file("svc/link.txt", PAYLOAD)
        self.scene.commit()
        self.link = self.scene.root / "svc" / "link.txt"

    def _read(self, *, max_bytes: Optional[int] = None):
        kwargs = {} if max_bytes is None else {"max_bytes": max_bytes}
        return _read_path_content(
            self.scene.root,
            self.link,
            "working-tree",
            normalize=False,
            **kwargs,
        )

    def test_the_premise_an_unposed_path_reads_back_as_its_file_content(self):
        """Without the pose the reader returns the bytes on disk, as mode 100644.

        Every "never dereferenced" claim below is the negation of this, so it
        has to be shown to be reachable first.
        """
        content = self._read()
        self.assertEqual(bytes(content), PAYLOAD.encode("utf-8"))
        self.assertEqual(content.git_mode, "100644")
        self.assertEqual(content.git_object_type, "blob")

    def test_a_symlink_is_hashed_as_its_link_target_text_and_not_dereferenced(self):
        pose = _PosedSymlink(self.link, LINK_TARGET)
        with pose:
            content = self._read()
        self.assertEqual(bytes(content), LINK_TARGET.encode("utf-8"))
        self.assertEqual(content.git_mode, "120000")
        self.assertEqual(content.git_object_type, "blob")
        self.assertEqual(content.source_size, len(LINK_TARGET.encode("utf-8")))
        self.assertEqual(pose.readlink_calls, [os.fspath(self.link)])
        self.assertNotEqual(bytes(content), PAYLOAD.encode("utf-8"))

    def test_the_content_is_exactly_fsencode_of_the_readlink_answer(self):
        targets = {
            "a bare name": "real.txt",
            "a relative path": "../shared/real.txt",
            "an absolute posix path": "/srv/shared/real.txt",
            "a name with spaces": "a name with spaces.txt",
            "a non-ascii name": "café/menu.txt",
        }
        for label, target in targets.items():
            with self.subTest(target=label):
                with _PosedSymlink(self.link, target):
                    content = self._read()
                self.assertEqual(bytes(content), os.fsencode(target))
                self.assertEqual(content.git_mode, "120000")

    def test_an_oversized_st_size_is_refused_before_readlink_runs(self):
        pose = _PosedSymlink(self.link, LINK_TARGET, st_size=4096)
        with pose:
            with self.assertRaises(GuardrailError) as raised:
                self._read(max_bytes=64)
        self.assertEqual(
            str(raised.exception),
            "Hash guardrail exceeded: file too large (4096 bytes) at svc/link.txt",
        )
        self.assertEqual(pose.readlink_calls, [])

    def test_an_st_size_under_reporting_the_target_is_caught_after_the_encode(self):
        """The second guard: st_size passes, the encoded target does not.

        This is the check the register recorded as unreachable. A link whose
        recorded size disagrees with its target - or one retargeted between the
        stat and the read - produces exactly this shape, and the message names
        the encoded length rather than the stat's.
        """
        target = "d/" * 40
        encoded_length = len(os.fsencode(target))
        self.assertEqual(encoded_length, 80)
        pose = _PosedSymlink(self.link, target, st_size=3)
        with pose:
            with self.assertRaises(GuardrailError) as raised:
                self._read(max_bytes=10)
        self.assertEqual(
            str(raised.exception),
            "Hash guardrail exceeded: file too large (80 bytes) at svc/link.txt",
        )
        self.assertNotIn("(3 bytes)", str(raised.exception))
        self.assertEqual(pose.readlink_calls, [os.fspath(self.link)])

    def test_a_target_utf8_cannot_encode_is_hashed_rather_than_rejected(self):
        """A lone surrogate is what a non-UTF-8 target looks like after readlink."""
        target = "caf\udcff/x"
        with self.assertRaises(UnicodeEncodeError):
            target.encode("utf-8")
        with _PosedSymlink(self.link, target):
            content = self._read()
        self.assertEqual(bytes(content), os.fsencode(target))
        self.assertEqual(content.git_mode, "120000")

    @unittest.skipIf(
        os.name == "nt",
        "surrogateescape is the POSIX filesystem error handler; Windows uses "
        "surrogatepass, so there are no undecodable target bytes to round-trip",
    )
    def test_undecodable_posix_target_bytes_round_trip_unchanged(self):
        raw = b"caf\xff/x"
        target = os.fsdecode(raw)
        with _PosedSymlink(self.link, target):
            content = self._read()
        self.assertEqual(bytes(content), raw)


class SymlinkVersusRegularFileIdentityTests(unittest.TestCase):
    """OBL-GIT-SOURCE-094: the same bytes under two modes are two identities."""

    def setUp(self):
        self.scene = Scenario()
        self.addCleanup(self.scene.close)
        self.scene.component(
            "svc", path="svc", provider="path-hash", boundary=["*.txt"]
        )
        self.scene.file("svc/real.txt", PAYLOAD)
        # Seed blob: the link-target text with no trailing newline.
        (self.scene.root / "svc").mkdir(parents=True, exist_ok=True)
        (self.scene.root / "svc" / "plain.txt").write_bytes(
            LINK_TARGET.encode("utf-8")
        )
        self.scene.commit()
        self.oid = _commit_symlink_entry(self.scene, "svc/link.txt", "svc/plain.txt")
        self.link = self.scene.root / "svc" / "link.txt"

    def test_the_premise_both_entries_carry_the_identical_blob(self):
        """Same object id under two modes, which is what makes the swap silent."""
        listing = dict(
            (line.split("\t")[1], line.split("\t")[0])
            for line in self.scene.git("ls-tree", "-r", "HEAD").splitlines()
        )
        self.assertEqual(listing["svc/link.txt"], f"120000 blob {self.oid}")
        self.assertEqual(listing["svc/plain.txt"], f"100644 blob {self.oid}")

    def test_the_premise_a_tracked_path_absent_from_disk_is_reported_deleted(self):
        """Nothing was written at svc/link.txt yet, so the comparison must say D."""
        self.assertEqual(
            _working_tree_name_status(self.scene.root, "HEAD"),
            [("D", "svc/link.txt")],
        )

    def test_the_premise_an_edited_regular_file_is_reported_modified(self):
        """M is reachable, so asserting T below is a real discrimination."""
        (self.scene.root / "svc" / "real.txt").write_bytes(b"edited payload\n")
        self.assertIn(
            ("M", "svc/real.txt"),
            _working_tree_name_status(self.scene.root, "HEAD"),
        )

    def test_replacing_a_symlink_with_a_regular_file_of_that_text_reports_T(self):
        self.link.write_bytes(LINK_TARGET.encode("utf-8"))
        self.assertEqual(
            _working_tree_name_status(self.scene.root, "HEAD"),
            [("T", "svc/link.txt")],
        )

    def test_the_swap_is_not_hidden_by_an_identical_content_digest(self):
        """The hazard the obligation names: an empty change list would be wrong."""
        self.link.write_bytes(LINK_TARGET.encode("utf-8"))
        changed = changed_paths_since_ref(self.scene.root, "HEAD", "working-tree")
        self.assertEqual(changed, ["svc/link.txt"])

    def test_a_trailing_newline_on_the_replacement_is_still_reported_T(self):
        """Content differing too must not downgrade the verdict to M."""
        self.link.write_bytes(LINK_TARGET.encode("utf-8") + b"\n")
        self.assertEqual(
            _working_tree_name_status(self.scene.root, "HEAD"),
            [("T", "svc/link.txt")],
        )

    def test_a_symlink_and_a_regular_file_of_the_same_text_get_different_digests(self):
        with self._two_components(link_mode=True) as digests:
            linked, plain = digests
        self.assertNotEqual(linked, plain)

    def test_the_premise_two_regular_files_of_the_same_text_share_a_digest(self):
        """Which proves the difference above is the mode, not the component name."""
        with self._two_components(link_mode=False) as digests:
            linked, plain = digests
        self.assertEqual(linked, plain)

    @contextlib.contextmanager
    def _two_components(self, *, link_mode: bool):
        """Two one-file components whose selector name and bytes are identical."""
        with Scenario() as scene:
            scene.component(
                "linked", path="linked", provider="path-hash", boundary=["f.txt"]
            )
            scene.component(
                "plain", path="plain", provider="path-hash", boundary=["f.txt"]
            )
            (scene.root / "plain").mkdir(parents=True, exist_ok=True)
            (scene.root / "plain" / "f.txt").write_bytes(LINK_TARGET.encode("utf-8"))
            if not link_mode:
                (scene.root / "linked").mkdir(parents=True, exist_ok=True)
                (scene.root / "linked" / "f.txt").write_bytes(
                    LINK_TARGET.encode("utf-8")
                )
            scene.commit()
            if link_mode:
                _commit_symlink_entry(scene, "linked/f.txt", "plain/f.txt")
            lockfile = scene.generate(source="head")
            yield (
                lockfile["components"]["linked"]["fingerprints"]["boundary"],
                lockfile["components"]["plain"]["fingerprints"]["boundary"],
            )


class DanglingSymlinkVisibilityTests(unittest.TestCase):
    """OBL-GIT-SOURCE-095: a link whose target is gone is not a deleted file."""

    def setUp(self):
        self.scene = Scenario()
        self.addCleanup(self.scene.close)
        self.scene.component(
            "svc", path="svc", provider="path-hash", boundary=["*.txt"]
        )
        self.scene.file("svc/other.txt", "kept\n")
        (self.scene.root / "svc").mkdir(parents=True, exist_ok=True)
        (self.scene.root / "svc" / "seed.txt").write_bytes(b"gone.txt")
        self.scene.commit()
        _commit_symlink_entry(self.scene, "svc/link.txt", "svc/seed.txt")
        self.link = self.scene.root / "svc" / "link.txt"
        self.head_digest = self.scene.digest("svc", "boundary", source="head")

    def _pose(self) -> _PosedSymlink:
        return _PosedSymlink(self.link, "gone.txt", dangling=True)

    def test_the_premise_an_unposed_missing_entry_is_dropped_from_the_file_list(self):
        """exists() and is_symlink() are both False, so the filter removes it."""
        self.assertEqual(
            _list_files_for_source(self.scene.root, "svc", "working-tree"),
            ["svc/other.txt", "svc/seed.txt"],
        )

    def test_the_premise_an_unposed_missing_entry_is_reported_deleted(self):
        self.assertEqual(
            _working_tree_name_status(self.scene.root, "HEAD"),
            [("D", "svc/link.txt")],
        )

    def test_a_dangling_symlink_stays_in_the_working_tree_file_list(self):
        """The `or (repo_root / f).is_symlink()` disjunct is the only thing
        keeping it, since exists() follows the link and answers False."""
        with self._pose() as pose:
            files = _list_files_for_source(self.scene.root, "svc", "working-tree")
            self.assertFalse(self.link.exists())
            self.assertTrue(self.link.is_symlink())
        self.assertEqual(files, ["svc/link.txt", "svc/other.txt", "svc/seed.txt"])
        self.assertEqual(pose.readlink_calls, [])

    def test_a_dangling_symlink_is_not_reported_as_deleted(self):
        with self._pose():
            self.assertEqual(_working_tree_name_status(self.scene.root, "HEAD"), [])

    def test_a_dangling_symlink_leaves_the_changed_path_set_empty(self):
        with self._pose():
            self.assertEqual(
                changed_paths_since_ref(self.scene.root, "HEAD", "working-tree"),
                [],
            )

    def test_a_dangling_symlink_hashes_as_its_link_target_bytes(self):
        """Which is why the working-tree digest still equals the head digest."""
        with self._pose() as pose:
            working = self.scene.digest("svc", "boundary", source="working-tree")
        self.assertEqual(working, self.head_digest)
        self.assertIn(os.fspath(self.link), pose.readlink_calls)


class MutationGateStrictnessTests(unittest.TestCase):
    """OBL-GIT-SOURCE-143: add and remove judge validity by a weaker rule."""

    @classmethod
    def setUpClass(cls):
        cls.catalogue_scene = Scenario()
        scene = cls.catalogue_scene
        scene.config["components"] = json.loads(json.dumps(FACET_CATALOGUE))
        for name in FACET_CATALOGUE:
            scene.file(f"{name}/a.txt", "a\n")
        scene.commit()
        cls.base_config = json.loads(
            (scene.root / "boundary.config.json").read_text(encoding="utf-8")
        )

    @classmethod
    def tearDownClass(cls):
        cls.catalogue_scene.close()

    def _with_slice(self, members: List[str], mode: str) -> dict:
        config = json.loads(json.dumps(self.base_config))
        config["slices"] = {"wave": {"mode": mode, "components": list(members)}}
        return config

    def _gates(self, config: dict):
        root = self.catalogue_scene.root
        # Exactly the call `_cmd_add` and `_cmd_remove` make.
        weak = set(validate_config(config, root))
        # Exactly the call `_cmd_validate_config` makes without --allow-partial.
        strict = set(
            validate_config(
                config,
                root,
                allow_custom_providers=False,
                require_slice_facets=True,
            )
        )
        return weak, strict

    def test_the_premise_the_catalogue_itself_is_valid_under_both_gates(self):
        weak, strict = self._gates(self.base_config)
        self.assertEqual(weak, set())
        self.assertEqual(strict, set())

    @PROFILE
    @given(
        members=st.lists(
            st.sampled_from(sorted(FACET_CATALOGUE)),
            min_size=1,
            max_size=3,
            unique=True,
        ),
        mode=st.sampled_from(SLICE_MODES),
    )
    def test_the_mutation_gate_is_always_the_weaker_of_the_two_verdicts(
        self, members, mode
    ):
        weak, strict = self._gates(self._with_slice(members, mode))
        self.assertLessEqual(weak, strict)
        unsupplied = {
            name
            for name in members
            if mode != "exact"
            and mode not in declared_facets(FACET_CATALOGUE[name])
        }
        self.assertEqual(bool(strict - weak), bool(unsupplied), (members, mode))
        for name in sorted(unsupplied):
            self.assertTrue(
                any(
                    f"Slice 'wave' mode '{mode}'" in error and f"'{name}'" in error
                    for error in strict - weak
                ),
                (members, mode, sorted(strict - weak)),
            )

    def _defective_repository(self) -> Scenario:
        """A config `validate-config` rejects and `add`/`remove` do not."""
        scene = Scenario()
        self.addCleanup(scene.close)
        scene.component("svc", path="svc", provider="path-hash", boundary=["*.txt"])
        scene.component("doomed", path="doomed", provider="path-hash", boundary=["*.txt"])
        scene.file("svc/a.txt", "a\n")
        scene.file("doomed/a.txt", "a\n")
        scene.file("extra/b.txt", "b\n")
        scene.slice("wave", mode="behavior", components=["svc"])
        scene.commit()
        return scene

    def test_the_premise_validate_config_rejects_the_defective_config(self):
        scene = self._defective_repository()
        result = run_cli(scene.root, "validate-config")
        self.assertEqual(result.returncode, 2)
        self.assertIn("CONFIG INVALID (1 issues):", result.stdout)
        self.assertIn(
            "Slice 'wave' mode 'behavior' requires behavior digest from "
            "component 'svc' to supply that facet, but the component has no "
            "non-empty behavior.paths",
            result.stdout,
        )

    def test_add_rewrites_a_config_that_validate_config_rejects(self):
        scene = self._defective_repository()
        before = (scene.root / "boundary.config.json").read_text(encoding="utf-8")
        result = run_cli(scene.root, "add", "extra", "extra")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Added component 'extra' at path 'extra'", result.stdout)
        self.assertIn("Run: boundver generate --components extra", result.stdout)
        after = (scene.root / "boundary.config.json").read_text(encoding="utf-8")
        self.assertNotEqual(after, before)
        self.assertIn("extra", json.loads(after)["components"])

    def test_remove_rewrites_a_config_that_validate_config_rejects(self):
        scene = self._defective_repository()
        result = run_cli(scene.root, "remove", "doomed")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Removed component 'doomed'", result.stdout)
        self.assertIn("Run: boundver generate", result.stdout)
        after = json.loads(
            (scene.root / "boundary.config.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("doomed", after["components"])

    def test_the_failure_arrives_at_the_generate_the_diagnostic_recommended(self):
        """The promise `add` prints is wrong one command later, and blames extra."""
        scene = self._defective_repository()
        self.assertEqual(run_cli(scene.root, "add", "extra", "extra").returncode, 0)
        result = run_cli(scene.root, "generate", "--components", "extra")
        self.assertEqual(result.returncode, 2)
        self.assertIn("ERROR: Config is invalid (1 issues):", result.stderr)
        self.assertIn("Slice 'wave' mode 'behavior'", result.stderr)

    def test_the_mutation_gate_matches_validate_config_allow_partial(self):
        """Naming which validity notion `add` actually implements."""
        scene = self._defective_repository()
        self.assertEqual(
            run_cli(scene.root, "validate-config", "--allow-partial").returncode, 0
        )
        self.assertEqual(run_cli(scene.root, "validate-config").returncode, 2)
        self.assertEqual(run_cli(scene.root, "add", "extra", "extra").returncode, 0)


class ConfigOutsideRepositoryTests(unittest.TestCase):
    """OBL-GIT-SOURCE-144: --config names one tree, repo_root another."""

    def setUp(self):
        self.scene = Scenario()
        self.addCleanup(self.scene.close)
        self.scene.component("svc", path="svc", provider="implicit")
        self.scene.file("svc/a.txt", "a\n")
        self.scene.file("inrepo/x.txt", "x\n")
        self.scene.commit()

    @staticmethod
    def _valid_config(project: str) -> str:
        return (
            json.dumps(
                {
                    "project": project,
                    "components": {
                        "svc": {
                            "path": "svc",
                            "version_source": None,
                            "boundary": {"provider": "implicit", "paths": []},
                        }
                    },
                },
                indent=2,
            )
            + "\n"
        )

    def _outside_directory(self) -> Path:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        outside = Path(holder.name)
        (outside / "boundary.config.json").write_text(
            self._valid_config("outside"), encoding="utf-8"
        )
        (outside / "svc").mkdir(exist_ok=True)
        (outside / "besideonly").mkdir(exist_ok=True)
        (outside / "besideonly" / "y.txt").write_text("y", encoding="utf-8")
        return outside

    def test_the_premise_add_through_config_really_rewrites_the_named_file(self):
        """In-repository first, so "rewritten" below is a demonstrated effect."""
        before = (self.scene.root / "boundary.config.json").read_text(encoding="utf-8")
        result = run_cli(
            self.scene.root, "add", "inrepo", "inrepo", "--config",
            "boundary.config.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        after = (self.scene.root / "boundary.config.json").read_text(encoding="utf-8")
        self.assertNotEqual(after, before)

    def test_an_absolute_config_outside_the_repository_is_read_and_rewritten(self):
        outside = self._outside_directory()
        away = outside / "boundary.config.json"
        before = away.read_text(encoding="utf-8")
        result = run_cli(
            self.scene.root, "add", "inrepo", "inrepo", "--config", str(away)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Added component 'inrepo' at path 'inrepo'", result.stdout)
        after = json.loads(away.read_text(encoding="utf-8"))
        self.assertNotEqual(away.read_text(encoding="utf-8"), before)
        self.assertEqual(after["project"], "outside")
        self.assertIn("inrepo", after["components"])
        # Nothing was created in the repository the paths were checked against.
        self.assertNotIn(
            "inrepo",
            json.loads(
                (self.scene.root / "boundary.config.json").read_text(encoding="utf-8")
            )["components"],
        )

    def test_declared_paths_resolve_against_the_repository_not_the_config(self):
        """Both directions, because either alone is consistent with the other tree."""
        outside = self._outside_directory()
        away = outside / "boundary.config.json"
        accepted = run_cli(
            self.scene.root, "add", "inrepo", "inrepo", "--config", str(away)
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        refused = run_cli(
            self.scene.root, "add", "besideonly", "besideonly", "--config", str(away)
        )
        self.assertEqual(refused.returncode, 2)
        self.assertIn(
            "ERROR: Adding 'besideonly' would leave an invalid config:",
            refused.stderr,
        )
        self.assertIn(
            "Component 'besideonly' path not found or not a directory: besideonly",
            refused.stderr,
        )
        self.assertTrue((outside / "besideonly").is_dir())

    def _sibling_directory(self) -> Path:
        sibling = self.scene.root.parent / (self.scene.root.name + "-sib")
        sibling.mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, sibling, ignore_errors=True)
        (sibling / "svc").mkdir(exist_ok=True)
        return sibling

    def test_a_relative_parent_config_is_parsed_before_the_write_stage_refuses(self):
        """The read happens first: a broken file complains about JSON, not traversal."""
        sibling = self._sibling_directory()
        (sibling / "boundary.config.json").write_text("{ not json", encoding="utf-8")
        relative = f"../{sibling.name}/boundary.config.json"
        result = run_cli(self.scene.root, "add", "n", "inrepo", "--config", relative)
        self.assertEqual(result.returncode, 2)
        self.assertIn("JSON parse error in", result.stderr)
        self.assertIn(sibling.name, result.stderr)
        self.assertNotIn("parent-directory traversal", result.stderr)

    def test_a_relative_parent_config_is_refused_only_at_the_write_stage(self):
        sibling = self._sibling_directory()
        target = sibling / "boundary.config.json"
        target.write_text(self._valid_config("sibling"), encoding="utf-8")
        before = target.read_text(encoding="utf-8")
        relative = f"../{sibling.name}/boundary.config.json"
        result = run_cli(self.scene.root, "add", "n", "inrepo", "--config", relative)
        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "ERROR: add failed: Output path must not contain parent-directory "
            "traversal:",
            result.stderr,
        )
        self.assertEqual(target.read_text(encoding="utf-8"), before)
        self.assertEqual(
            sorted(entry.name for entry in sibling.iterdir()),
            ["boundary.config.json", "svc"],
        )


class ConfigOutputLeafTests(unittest.TestCase):
    """OBL-GIT-SOURCE-145: nothing writes through a non-regular config path."""

    def setUp(self):
        self.scene = Scenario()
        self.addCleanup(self.scene.close)
        self.scene.component("svc", path="svc", provider="implicit")
        self.scene.file("svc/a.txt", "a\n")
        self.scene.file("extra/b.txt", "b\n")
        self.scene.commit()
        self.config_path = self.scene.root / "boundary.config.json"

    def _replace_config_with_a_directory(self) -> Path:
        self.config_path.unlink()
        self.config_path.mkdir()
        (self.config_path / "canary.txt").write_text("keep", encoding="utf-8")
        return self.config_path

    @staticmethod
    def _leaf_is_a_reparse_point():
        """Force the reparse-point verdict this host cannot produce naturally.

        Windows refuses symlink creation without SeCreateSymbolicLinkPrivilege,
        so the only way to reach the branch that a symlinked config would take
        is to make the predicate answer for the leaf. Directory ancestors stay
        untouched because only regular files are answered for.
        """
        real = core._is_windows_reparse_point

        def verdict(identity):
            if stat.S_ISREG(identity.st_mode):
                return True
            return real(identity)

        return mock.patch.object(core, "_is_windows_reparse_point", verdict)

    def test_the_premise_a_successful_add_rewrites_and_leaves_no_sidecar(self):
        before = self.config_path.read_text(encoding="utf-8")
        result = run_cli(self.scene.root, "add", "extra", "extra")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.config_path.read_text(encoding="utf-8"), before)
        self.assertEqual(
            sorted(entry.name for entry in self.scene.root.iterdir()),
            [".git", "boundary.config.json", "extra", "svc"],
        )

    def test_init_refuses_without_force_when_the_config_path_is_occupied(self):
        """exists() is the guard, and it answers yes for a directory too."""
        self._replace_config_with_a_directory()
        result = run_cli(self.scene.root, "init")
        self.assertEqual(result.returncode, 2)
        self.assertIn("ERROR: Config already exists:", result.stderr)

    def test_init_force_refuses_a_non_regular_config_path_and_leaves_it_whole(self):
        occupied = self._replace_config_with_a_directory()
        result = run_cli(self.scene.root, "init", "--force")
        self.assertEqual(result.returncode, 2)
        self.assertIn("ERROR: init failed: Cannot inspect output path:", result.stderr)
        self.assertTrue(occupied.is_dir())
        self.assertEqual(
            (occupied / "canary.txt").read_text(encoding="utf-8"), "keep"
        )
        self.assertEqual(
            sorted(entry.name for entry in occupied.iterdir()), ["canary.txt"]
        )

    def test_the_write_guard_fires_even_when_the_overwrite_guard_saw_nothing(self):
        """The dangling-link ordering: exists() False, so --force is never demanded.

        A dangling symlink is the natural way to reach this - exists() follows
        the link and answers False while the leaf is still not a regular file.
        Here exists() is answered False for the config path alone while a
        directory really sits there, so the refusal has to come from the
        write-time leaf inspection or not at all.
        """
        occupied = self._replace_config_with_a_directory()
        key = os.path.normcase(os.fspath(occupied))

        def fake_exists(self, **kwargs):
            if os.path.normcase(os.fspath(self)) == key:
                return False
            return _REAL_EXISTS(self, **kwargs)

        with mock.patch.object(Path, "exists", fake_exists):
            result = run_cli_in_process(self.scene.root, "init")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Config already exists", result.stderr)
        self.assertIn("ERROR: init failed: Cannot inspect output path:", result.stderr)
        self.assertEqual(
            (occupied / "canary.txt").read_text(encoding="utf-8"), "keep"
        )

    def test_add_refuses_a_reparse_point_config_it_had_already_read(self):
        before = self.config_path.read_text(encoding="utf-8")
        with self._leaf_is_a_reparse_point():
            result = run_cli_in_process(self.scene.root, "add", "extra", "extra")
        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "ERROR: add failed: Output path must be a regular file, not a "
            "symlink, junction, or reparse point: ",
            result.stderr,
        )
        # The path it refused to write is the path it read and validated.
        self.assertIn(str(self.config_path).replace("\\", "\\\\"), result.stderr)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)
        self.assertEqual(
            sorted(entry.name for entry in self.scene.root.iterdir()),
            [".git", "boundary.config.json", "extra", "svc"],
        )

    def test_remove_refuses_a_reparse_point_config_it_had_already_read(self):
        self.scene.component("extra", path="extra", provider="implicit")
        self.scene.commit()
        before = self.config_path.read_text(encoding="utf-8")
        with self._leaf_is_a_reparse_point():
            result = run_cli_in_process(self.scene.root, "remove", "extra")
        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "ERROR: remove failed: Output path must be a regular file, not a "
            "symlink, junction, or reparse point: ",
            result.stderr,
        )
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)

    def test_init_force_refuses_a_reparse_point_config(self):
        before = self.config_path.read_text(encoding="utf-8")
        with self._leaf_is_a_reparse_point():
            result = run_cli_in_process(self.scene.root, "init", "--force")
        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "ERROR: init failed: Output path must be a regular file, not a "
            "symlink, junction, or reparse point: ",
            result.stderr,
        )
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)

    def test_the_json_only_guard_refuses_before_the_file_is_parsed(self):
        """Ordering, for all three commands: a broken YAML is never read."""
        broken = self.scene.root / "boundary.config.yaml"
        broken.write_text("this: [is not: valid", encoding="utf-8")
        invocations = {
            "add": ["add", "n", "svc", "--config", "boundary.config.yaml"],
            "remove": ["remove", "svc", "--config", "boundary.config.yaml"],
            "init": ["init", "--out", "boundary.config.yaml", "--force"],
        }
        for command, arguments in invocations.items():
            with self.subTest(command=command):
                result = run_cli(self.scene.root, *arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(
                    result.stderr.strip(),
                    f"ERROR: `boundver {command}` only writes JSON configs. "
                    "Use boundary.config.json or edit your YAML/TOML config "
                    "directly.",
                )
        self.assertEqual(
            broken.read_text(encoding="utf-8"), "this: [is not: valid"
        )


if __name__ == "__main__":
    unittest.main()
