"""What a path is, according to the source you asked boundver to read.

A digest is over bytes with a mode and a type attached, so the first question
about any path is what kind of thing it is. Three rules follow from that. An
input that has no defensible mode is refused rather than guessed at. A regular
file's mode comes from the index when the filesystem cannot be trusted to
carry it, and from the filesystem otherwise. And a lock read out of a captured
tree must actually be a blob in that tree, whatever happens to sit at the same
path on disk.

Covers OBL-GIT-SOURCE-002, OBL-GIT-SOURCE-003 and OBL-GIT-SOURCE-012.
"""

from __future__ import annotations

import os
import stat
import unittest

from boundver._git import GitTreeEntry, _working_tree_mode

from tests._parity import run_cli
from tests._scenarios import Scenario, requires_symlinks

COULD_NOT_CHECK = 2

#: Git modes a lock path must never have in a captured tree.
UNREADABLE_LOCK_MODES = {"120000": "blob", "160000": "commit"}


def _entry(mode: str) -> GitTreeEntry:
    return GitTreeEntry(
        path="svc/main.py", mode=mode, object_type="blob", oid="0" * 40
    )


class _Tracked:
    """A repository with one tracked regular file."""

    def __init__(self) -> None:
        scene = Scenario()
        scene.component("svc", path="svc", provider="leaf")
        scene.file("svc/main.py", "x\n")
        scene.commit()
        self.scene = scene

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def mode(self, tracked=None, *, core_filemode=True):
        return _working_tree_mode(
            self.scene.root, "svc/main.py", tracked, core_filemode=core_filemode
        )


class UnsupportedTypeTests(unittest.TestCase):
    """OBL-GIT-SOURCE-002: no mode means no digest."""

    def test_a_regular_file_has_a_mode(self):
        """The premise: the classifier answers for the ordinary case."""
        with _Tracked() as repo:
            self.assertEqual(repo.mode(), ("100644", "blob"))

    def test_a_directory_is_refused(self):
        with _Tracked() as repo:
            (repo.scene.root / "svc" / "adir").mkdir()
            with self.assertRaises(ValueError) as raised:
                _working_tree_mode(repo.scene.root, "svc/adir")
            self.assertIn("Unsupported working-tree file type", str(raised.exception))

    def test_a_missing_path_is_refused_with_its_own_message(self):
        """The neighbouring failure, so the refusal is not one branch wide."""
        with _Tracked() as repo:
            with self.assertRaises(ValueError) as raised:
                _working_tree_mode(repo.scene.root, "svc/absent.py")
            self.assertIn("File disappeared while hashing", str(raised.exception))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "this host cannot create a FIFO")
    def test_a_fifo_is_refused(self):
        with _Tracked() as repo:
            fifo = repo.scene.root / "svc" / "pipe"
            os.mkfifo(fifo)
            with self.assertRaises(ValueError) as raised:
                _working_tree_mode(repo.scene.root, "svc/pipe")
            self.assertIn("Unsupported working-tree file type", str(raised.exception))


class FilemodeResolutionTests(unittest.TestCase):
    """OBL-GIT-SOURCE-003: where a regular file's mode comes from."""

    def test_the_tracked_mode_wins_when_the_filesystem_cannot_carry_it(self):
        with _Tracked() as repo:
            on_disk = stat.S_IMODE(
                (repo.scene.root / "svc" / "main.py").stat().st_mode
            )
            self.assertFalse(on_disk & stat.S_IXUSR, oct(on_disk))
            self.assertEqual(
                repo.mode(_entry("100755"), core_filemode=False), ("100755", "blob")
            )
            self.assertEqual(
                repo.mode(_entry("100644"), core_filemode=False), ("100644", "blob")
            )

    def test_the_filesystem_wins_when_it_can_carry_it(self):
        with _Tracked() as repo:
            self.assertEqual(
                repo.mode(_entry("100755"), core_filemode=True), ("100644", "blob")
            )

    def test_an_untracked_path_takes_the_filesystem_mode(self):
        with _Tracked() as repo:
            self.assertEqual(repo.mode(None, core_filemode=False), ("100644", "blob"))

    def test_a_tracked_mode_that_is_not_a_file_mode_is_ignored(self):
        """Only 100644 and 100755 are carried over; a symlink entry is not."""
        with _Tracked() as repo:
            for mode in ("120000", "160000"):
                with self.subTest(tracked=mode):
                    self.assertEqual(
                        repo.mode(_entry(mode), core_filemode=False),
                        ("100644", "blob"),
                    )

    @requires_symlinks
    def test_a_symlink_on_disk_is_its_own_mode(self):
        """The branch that runs before any of this, tracked entry or not."""
        with _Tracked() as repo:
            link = repo.scene.root / "svc" / "link"
            link.symlink_to(repo.scene.root / "svc" / "main.py")
            self.assertEqual(
                _working_tree_mode(repo.scene.root, "svc/link"), ("120000", "blob")
            )
            self.assertEqual(
                _working_tree_mode(
                    repo.scene.root, "svc/link", _entry("100755"),
                    core_filemode=False,
                ),
                ("120000", "blob"),
            )


class SourceBackedLockTests(unittest.TestCase):
    """OBL-GIT-SOURCE-012: a lock in a tree must be a blob in that tree."""

    def _repository_with_lock_at(self, mode: str):
        scene = Scenario()
        scene.component("svc", path="svc", provider="leaf")
        scene.file("svc/main.py", "x\n")
        scene.commit()
        assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
        scene.commit("lock")
        clean = run_cli(scene.root, "verify", "--source", "head")
        assert clean.returncode == 0, clean.stdout
        oid = (
            "1" * 40 if mode == "160000"
            else scene.git("rev-parse", "HEAD:boundary.lock.json")
        )
        scene.git("rm", "--cached", "boundary.lock.json")
        scene.git(
            "update-index", "--add", "--cacheinfo",
            f"{mode},{oid},boundary.lock.json",
        )
        scene.git("commit", "-m", "retyped lock")
        return scene

    def test_a_retyped_lock_is_refused_in_every_captured_source(self):
        for mode, object_type in UNREADABLE_LOCK_MODES.items():
            for source in ("head", "index"):
                with self.subTest(mode=mode, source=source):
                    scene = self._repository_with_lock_at(mode)
                    try:
                        result = run_cli(scene.root, "verify", "--source", source)
                        self.assertEqual(result.returncode, COULD_NOT_CHECK)
                        self.assertIn(
                            f"Lockfile path must be a regular file in captured "
                            f"{source} source",
                            result.stderr,
                        )
                        self.assertIn(f"mode={mode}", result.stderr)
                        self.assertIn(f"type={object_type}", result.stderr)
                    finally:
                        scene.close()

    def test_the_working_tree_content_is_not_substituted(self):
        """A valid lock on disk does not rescue the captured tree."""
        scene = self._repository_with_lock_at("160000")
        try:
            self.assertTrue((scene.root / "boundary.lock.json").is_file())
            self.assertIn(
                "schema",
                (scene.root / "boundary.lock.json").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                run_cli(scene.root, "verify", "--source", "head").returncode,
                COULD_NOT_CHECK,
            )
            self.assertEqual(
                run_cli(scene.root, "verify", "--source", "working-tree").returncode,
                0,
            )
        finally:
            scene.close()


if __name__ == "__main__":
    unittest.main()
