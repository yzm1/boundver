"""Integration contracts for caller-bounded source reads."""

from __future__ import annotations

import os
import subprocess
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from boundver._config import load_config_file
from boundver._hashing import _ModeAwareBytes
from boundver._lockfile import _SourceAccessor
from boundver._utils import (
    ConfigError,
    GuardrailError,
    _iter_bounded_filesystem_paths,
)
from tests._repo_fixtures import commit_all, init_git_repo


class SourceAccessorBoundTests(unittest.TestCase):
    def _repository(self, root: Path) -> None:
        init_git_repo(root)
        (root / "data.bin").write_bytes(b"12345678")
        commit_all(root, "fixture")

    def test_head_read_honors_provider_remaining_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._repository(root)
            accessor = _SourceAccessor(root, "head")

            with self.assertRaisesRegex(GuardrailError, "Git blob too large"):
                accessor.read_file_limited("data.bin", 4)

            content = accessor.read_file_limited("data.bin", 8)
            self.assertEqual(content, b"12345678")
            self.assertEqual(content.git_mode, "100644")

    def test_working_tree_read_honors_provider_remaining_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._repository(root)
            accessor = _SourceAccessor(root, "working-tree")

            with self.assertRaisesRegex(GuardrailError, "file too large"):
                accessor.read_file_limited("data.bin", 4)

            self.assertEqual(
                accessor.read_file_limited("data.bin", 8),
                b"12345678",
            )

    def test_working_tree_symlink_size_is_checked_before_readlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accessor = _SourceAccessor(root, "working-tree")
            link_stat = types.SimpleNamespace(
                st_dev=1,
                st_ino=2,
                st_size=2,
                st_mtime_ns=3,
                st_mode=stat.S_IFLNK | 0o777,
            )
            with (
                patch.object(Path, "lstat", return_value=link_stat),
                patch("boundver._hashing.os.readlink") as readlink,
            ):
                with self.assertRaisesRegex(
                    GuardrailError,
                    "file too large",
                ):
                    accessor.read_file_limited("link", 1)
            readlink.assert_not_called()

    def test_working_tree_symlink_identity_change_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accessor = _SourceAccessor(root, "working-tree")
            before = types.SimpleNamespace(
                st_dev=1,
                st_ino=2,
                st_size=1,
                st_mtime_ns=3,
                st_mode=stat.S_IFLNK | 0o777,
            )
            after = types.SimpleNamespace(
                st_dev=1,
                st_ino=9,
                st_size=1,
                st_mtime_ns=3,
                st_mode=stat.S_IFLNK | 0o777,
            )
            with (
                patch.object(Path, "lstat", side_effect=[before, after]),
                patch("boundver._hashing.os.readlink", return_value="x"),
            ):
                with self.assertRaisesRegex(ValueError, "changed while hashing"):
                    accessor.read_file_limited("link", 1)

    def test_working_tree_type_change_before_content_read_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accessor = _SourceAccessor(root, "working-tree")
            regular = types.SimpleNamespace(
                st_dev=1,
                st_ino=2,
                st_size=1,
                st_mtime_ns=3,
                st_mode=stat.S_IFREG | 0o644,
            )
            symlink = types.SimpleNamespace(
                st_dev=1,
                st_ino=9,
                st_size=1,
                st_mtime_ns=4,
                st_mode=stat.S_IFLNK | 0o777,
            )
            with (
                patch.object(Path, "lstat", side_effect=[regular, symlink]),
                patch(
                    "boundver._hashing._read_bounded_path_bytes",
                    return_value=b"x",
                ),
            ):
                with self.assertRaisesRegex(ValueError, "changed while hashing"):
                    accessor.read_file_limited("file", 1)

    def test_working_tree_rejects_symlink_directory_ancestor(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repo"
            external = base / "external"
            root.mkdir()
            external.mkdir()
            (external / "data.bin").write_bytes(b"external")
            try:
                (root / "svc").symlink_to(external, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")

            accessor = _SourceAccessor(root, "working-tree")
            with self.assertRaisesRegex(ValueError, "ancestor"):
                accessor.read_file_limited("svc/data.bin", 8)

    @unittest.skipUnless(os.name == "nt", "NTFS junction behavior is Windows-only")
    def test_working_tree_rejects_junction_directory_ancestor(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repo"
            external = base / "external"
            root.mkdir()
            external.mkdir()
            (external / "data.bin").write_bytes(b"external")
            junction = root / "svc"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
                capture_output=True,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"junctions unavailable: {created.stderr.strip()}")

            try:
                accessor = _SourceAccessor(root, "working-tree")
                with self.assertRaisesRegex(ValueError, "reparse point"):
                    accessor.read_file_limited("svc/data.bin", 8)
            finally:
                if junction.exists():
                    junction.rmdir()

    @unittest.skipUnless(os.name == "nt", "NTFS junction behavior is Windows-only")
    def test_filesystem_traversal_does_not_descend_junction(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repo"
            external = base / "external"
            root.mkdir()
            external.mkdir()
            (external / "secret.txt").write_text("external", encoding="utf-8")
            junction = root / "linked"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
                capture_output=True,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"junctions unavailable: {created.stderr.strip()}")

            try:
                discovered = [
                    path.relative_to(root).as_posix()
                    for path in _iter_bounded_filesystem_paths(
                        root,
                        recursive=True,
                        max_entries=10,
                        exceeded_message="too many entries",
                    )
                ]
                self.assertIn("linked", discovered)
                self.assertNotIn("linked/secret.txt", discovered)
            finally:
                if junction.exists():
                    junction.rmdir()

    def test_version_read_rechecks_symlink_mode_after_content_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accessor = _SourceAccessor(root, "working-tree")
            symlink_data = _ModeAwareBytes(b"target", "120000", "blob")
            with (
                patch.object(Path, "is_symlink", return_value=False),
                patch.object(
                    accessor,
                    "read_file_limited",
                    return_value=symlink_data,
                ),
            ):
                with self.assertRaisesRegex(ConfigError, "must not be a symlink"):
                    accessor.version_read_file("version.json")

    def test_disk_config_loader_uses_its_own_hard_read_ceiling(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "boundary.config.json"
            path.write_bytes(b'{"project":"too large"}')

            with patch("boundver._config.MAX_CONFIG_BYTES", 8):
                with self.assertRaisesRegex(ConfigError, "8-byte limit"):
                    load_config_file(path)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
