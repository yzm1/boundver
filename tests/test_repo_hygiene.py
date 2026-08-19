"""The release gate rejects repository debris and portability hazards."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_repo_hygiene.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("check_repo_hygiene", SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError(f"cannot import {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


def _repo() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "hygiene@example.invalid")
    _git(root, "config", "user.name", "Hygiene Test")
    return temporary, root


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(root)],
        capture_output=True,
        text=True,
    )


class RepositoryHygieneTests(unittest.TestCase):
    def test_git_capture_and_tracked_record_limits_fail_closed(self):
        hygiene = _load_script()
        temporary, root = _repo()
        with temporary:
            (root / "README.md").write_text("portable\n", encoding="utf-8")
            _git(root, "add", "README.md")
            with mock.patch.object(
                hygiene, "MAX_GIT_OUTPUT_BYTES", 8
            ), self.assertRaisesRegex(RuntimeError, "stdout exceeds the 8-byte limit"):
                hygiene._tracked_entries(root)

            with mock.patch.object(
                hygiene, "MAX_GIT_DIAGNOSTIC_BYTES", 4
            ), self.assertRaisesRegex(RuntimeError, "stderr exceeds the 4-byte limit"):
                hygiene._git(root, "not-a-real-git-command")

        prefix = b"100644 " + b"1" * 40 + b" 0\t"
        cases = (
            (
                "record",
                prefix + b"file.md\0",
                {"MAX_TRACKED_RECORD_BYTES": len(prefix) + 6},
            ),
            (
                "path",
                prefix + b"file.md\0",
                {"MAX_TRACKED_PATH_BYTES": 6},
            ),
            (
                "aggregate",
                prefix + b"one.md\0" + prefix + b"two.md\0",
                {"MAX_TRACKED_TOTAL_PATH_BYTES": 8},
            ),
            (
                "entry",
                prefix + b"one.md\0" + prefix + b"two.md\0",
                {"MAX_TRACKED_ENTRIES": 1},
            ),
        )
        for message, output, limits in cases:
            patches = [mock.patch.object(hygiene, name, value) for name, value in limits.items()]
            with self.subTest(limit=message), mock.patch.object(
                hygiene, "_git", return_value=output
            ), patches[0], self.assertRaisesRegex(RuntimeError, message):
                hygiene._tracked_entries(Path("repo"))

    def test_tracked_text_file_and_aggregate_reads_are_bounded(self):
        hygiene = _load_script()
        temporary, root = _repo()
        with temporary:
            (root / "a.md").write_bytes(b"one\n")
            (root / "b.md").write_bytes(b"two\n")
            _git(root, "add", ".")

            with mock.patch.object(hygiene, "MAX_TEXT_FILE_BYTES", 3):
                per_file_errors = hygiene.hygiene_errors(root)
            self.assertTrue(
                any("3-byte limit" in error for error in per_file_errors),
                per_file_errors,
            )

            with mock.patch.object(
                hygiene, "MAX_TEXT_FILE_BYTES", 8
            ), mock.patch.object(hygiene, "MAX_TEXT_TOTAL_BYTES", 6):
                aggregate_errors = hygiene.hygiene_errors(root)
            self.assertTrue(
                any("aggregate limit" in error for error in aggregate_errors),
                aggregate_errors,
            )

    def test_clean_portable_tree_passes(self):
        temporary, root = _repo()
        with temporary:
            (root / "src").mkdir()
            (root / "src" / "module.py").write_bytes(b"value = 1\n")
            (root / "scripts").mkdir()
            helper = root / "scripts" / "check.sh"
            helper.write_bytes(b"#!/usr/bin/env bash\nexit 0\n")
            _git(root, "add", "--chmod=+x", ".")
            _git(root, "update-index", "--chmod=-x", "src/module.py")
            result = _run(root)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_generated_artifact_and_bad_text_fail(self):
        temporary, root = _repo()
        with temporary:
            cache = root / "src" / "pkg" / "__pycache__"
            cache.mkdir(parents=True)
            (cache / "module.pyc").write_bytes(b"generated")
            (root / "README.md").write_bytes(b"bad trailing space \r\n")
            _git(root, "add", "-f", ".")
            _git(root, "update-index", "--chmod=-x", "README.md")
            result = _run(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("generated artifact is tracked", result.stderr)
            self.assertIn("contains CR characters", result.stderr)
            self.assertIn("trailing whitespace", result.stderr)

    def test_unexpected_executable_bit_fails(self):
        temporary, root = _repo()
        with temporary:
            path = root / "README.md"
            path.write_text("portable\n", encoding="utf-8")
            _git(root, "add", "--chmod=+x", "README.md")
            result = _run(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unexpected executable bit", result.stderr)


if __name__ == "__main__":
    unittest.main()
