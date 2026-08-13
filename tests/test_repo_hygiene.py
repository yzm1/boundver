"""The release gate rejects repository debris and portability hazards."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_repo_hygiene.py"


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
