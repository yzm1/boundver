"""What each source mode reports, and what it says when it cannot look.

Three modes read the same repository three ways, and the interesting cases are
where they should agree and do not. A submodule pointer that moved is one such
change: Git reports it, one mode reports it, and two do not. A capture that
fails is the other shape of the same problem - not a wrong answer but a wrong
severity, since "could not check" and "drifted" route differently in CI.

Covers OBL-GIT-SOURCE-010, OBL-GIT-SOURCE-013, OBL-GIT-SOURCE-014 and
OBL-GIT-SOURCE-015.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import boundver._lockfile as lockfile
from boundver._git import changed_paths_since_ref

from tests._parity import run_cli, run_cli_in_process
from tests._scenarios import Scenario, requires_symlinks

COULD_NOT_CHECK = 2

#: The two sentences a History line can end with.
DIRECT = "both immutable endpoint commits and trees; intervening history is not traversed"
MERGE_BASE = "a unique common ancestor and both immutable endpoint trees"


def _advance_submodule(root: Path, path: str) -> str:
    """Commit a change inside a nested repository and return its new head."""
    nested = root / path
    (nested / "content.txt").write_bytes(b"changed\n")
    for arguments in (
        ["add", "--all"],
        ["-c", "user.email=a@b", "-c", "user.name=a", "commit", "-m", "second"],
    ):
        subprocess.run(["git", *arguments], cwd=nested, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=nested, capture_output=True, text=True
    ).stdout.strip()


class SubmodulePointerTests(unittest.TestCase):
    """OBL-GIT-SOURCE-010: one changed-path set, whichever mode asks."""

    def _bumped(self):
        scene = Scenario()
        scene.component("svc", path="svc", provider="leaf")
        scene.file("svc/main.py", "x\n")
        scene.submodule("sub")
        scene.commit()
        base = scene.head()
        _advance_submodule(scene.root, "sub")
        scene.git("add", "sub")
        scene.git("commit", "-m", "bump submodule")
        return scene, base

    def test_git_itself_reports_the_path(self):
        """The premise and the oracle: the pointer really did move."""
        scene, base = self._bumped()
        try:
            self.assertEqual(
                scene.git("diff", "--name-only", base, "HEAD").splitlines(), ["sub"]
            )
        finally:
            scene.close()

    def test_the_working_tree_mode_reports_it(self):
        scene, base = self._bumped()
        try:
            self.assertEqual(
                changed_paths_since_ref(scene.root, base, "working-tree"), ["sub"]
            )
        finally:
            scene.close()

    def test_every_mode_reports_the_same_set(self):
        """Gitlink identity changes are visible without entering submodules."""
        scene, base = self._bumped()
        try:
            answers = {
                mode: changed_paths_since_ref(scene.root, base, mode)
                for mode in ("head", "index", "working-tree")
            }
            self.assertEqual(len(set(map(tuple, answers.values()))), 1, answers)
        finally:
            scene.close()

    def test_each_mode_reports_the_pointer(self):
        scene, base = self._bumped()
        try:
            self.assertEqual(changed_paths_since_ref(scene.root, base, "head"), ["sub"])
            self.assertEqual(changed_paths_since_ref(scene.root, base, "index"), ["sub"])
            self.assertEqual(
                changed_paths_since_ref(scene.root, base, "working-tree"), ["sub"]
            )
        finally:
            scene.close()

    def test_an_ordinary_file_change_is_reported_by_every_mode(self):
        """The contrast: the captured modes are not blind to everything."""
        scene = Scenario()
        try:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/main.py", "x\n")
            scene.commit()
            base = scene.head()
            scene.append_line("svc/main.py", "y\n")
            scene.commit("edit")
            for mode in ("head", "index", "working-tree"):
                with self.subTest(source=mode):
                    self.assertEqual(
                        changed_paths_since_ref(scene.root, base, mode),
                        ["svc/main.py"],
                    )
        finally:
            scene.close()


class CaptureFailureTests(unittest.TestCase):
    """OBL-GIT-SOURCE-013: a capture that failed is not drift."""

    def _locked(self):
        scene = Scenario()
        scene.component("svc", path="svc", provider="leaf")
        scene.file("svc/main.py", "x\n")
        scene.commit()
        assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
        scene.commit("lock")
        return scene

    def _verified_with_a_failing_capture(self, scene):
        def explodes(repo_root, source, **kwargs):
            raise ValueError("capture exploded")

        with patch.object(lockfile, "_capture_git_source_snapshot", explodes):
            return run_cli_in_process(scene.root, "verify", "--source", "working-tree")

    def test_the_repository_verifies_before_anything_is_broken(self):
        """The premise: the exit code is not 1 for an unrelated reason."""
        scene = self._locked()
        try:
            self.assertEqual(
                run_cli(scene.root, "verify", "--source", "working-tree").returncode, 0
            )
        finally:
            scene.close()

    def test_a_failed_capture_exits_could_not_check(self):
        """An unavailable source is an input failure, not ordinary drift."""
        scene = self._locked()
        try:
            self.assertEqual(
                self._verified_with_a_failing_capture(scene).returncode,
                COULD_NOT_CHECK,
            )
        finally:
            scene.close()

    def test_the_failure_keeps_its_diagnostic_and_usage_code(self):
        scene = self._locked()
        try:
            result = self._verified_with_a_failing_capture(scene)
            self.assertEqual(result.returncode, COULD_NOT_CHECK)
            self.assertIn(
                "Cannot capture working-tree source", result.stdout + result.stderr
            )
        finally:
            scene.close()


class ConfigSymlinkTests(unittest.TestCase):
    """OBL-GIT-SOURCE-014: a config read through a link is not this config."""

    @requires_symlinks
    def test_a_symlinked_config_is_refused_in_working_tree_mode(self):
        scene = Scenario()
        try:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/main.py", "x\n")
            scene.commit()
            real = scene.root / "real.config.json"
            real.write_bytes((scene.root / "boundary.config.json").read_bytes())
            (scene.root / "boundary.config.json").unlink()
            (scene.root / "boundary.config.json").symlink_to(real)
            result = run_cli(scene.root, "generate", "--source", "working-tree")
            self.assertEqual(result.returncode, COULD_NOT_CHECK, result.stdout)
            self.assertIn("symlink", result.stderr.lower())
        finally:
            scene.close()

    @requires_symlinks
    def test_a_symlinked_ancestor_is_refused_too(self):
        scene = Scenario()
        try:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/main.py", "x\n")
            (scene.root / "real").mkdir()
            (scene.root / "linked").symlink_to(scene.root / "real")
            scene.write_config()
            (scene.root / "real" / "boundary.config.json").write_bytes(
                (scene.root / "boundary.config.json").read_bytes()
            )
            scene.commit()
            result = run_cli(
                scene.root, "generate", "--source", "working-tree",
                "--config", "linked/boundary.config.json",
            )
            self.assertEqual(result.returncode, COULD_NOT_CHECK, result.stdout)
        finally:
            scene.close()


class ShallowHistoryTests(unittest.TestCase):
    """OBL-GIT-SOURCE-015: the History line names what it could not see."""

    def _reviewable(self, scene):
        scene.component("svc", path="svc", provider="leaf")
        scene.file("svc/main.py", "x\n")
        scene.commit()
        assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
        scene.commit("lock")
        scene.append_line("svc/main.py", "y\n")
        assert run_cli(scene.root, "generate", "--source", "working-tree").returncode == 0
        scene.commit("edit and relock")

    def _history_lines(self, root: Path, base: str, target: str):
        lines = {}
        for label, extra in (("direct", []), ("merge-base", ["--merge-base"])):
            result = run_cli(root, "review", "--base", base, "--target", target, *extra)
            assert result.returncode == 0, result.stderr
            found = [
                line.strip() for line in result.stdout.splitlines()
                if line.strip().startswith("History:")
            ]
            assert len(found) == 1, found
            lines[label] = found[0]
        return lines

    def test_a_complete_repository_says_complete(self):
        with Scenario() as scene:
            self._reviewable(scene)
            log = scene.git("log", "--format=%H").split()
            self.assertEqual(scene.git("rev-parse", "--is-shallow-repository"), "false")
            lines = self._history_lines(scene.root, log[1], log[0])
            self.assertEqual(lines["direct"], f"History: complete; {DIRECT}")
            self.assertEqual(lines["merge-base"], f"History: complete; {MERGE_BASE}")

    def test_a_shallow_clone_says_shallow(self):
        with Scenario() as scene:
            self._reviewable(scene)
            clone_root = Path(tempfile.mkdtemp()) / "shallow"
            subprocess.run(
                ["git", "clone", "--depth", "2", "--no-local", "--quiet",
                 scene.root.as_uri(), str(clone_root)],
                check=True, capture_output=True,
            )
            shallow = subprocess.run(
                ["git", "rev-parse", "--is-shallow-repository"], cwd=clone_root,
                capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(shallow, "true")
            log = subprocess.run(
                ["git", "log", "--format=%H"], cwd=clone_root,
                capture_output=True, text=True,
            ).stdout.split()
            lines = self._history_lines(clone_root, log[1], log[0])
            self.assertEqual(lines["direct"], f"History: shallow; {DIRECT}")
            self.assertEqual(lines["merge-base"], f"History: shallow; {MERGE_BASE}")


if __name__ == "__main__":
    unittest.main()
