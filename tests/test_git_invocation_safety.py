"""Three ways a Git call can answer about something other than what was asked.

A path handed to Git is only a path if it is passed as one. A cached answer is
only right while the thing it described has not moved. And a command that
fails has not told you the tree is clean. Each of these is a place where
boundver asks Git a question and takes the reply at face value.

Covers OBL-GIT-SOURCE-006, OBL-GIT-SOURCE-016, OBL-GIT-SOURCE-020 and
OBL-GIT-SOURCE-021.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import boundver._git as git
import boundver._hashing as hashing
from boundver._git import dirty_component_paths

from tests._parity import run_cli
from tests._scenarios import Scenario

COULD_NOT_CHECK = 2

#: A filename whose colon makes it a revision expression rather than a path.
AMBIGUOUS = "0:payload"


class _Repository:
    """A committed repository with one component and one unrelated file."""

    def __init__(self) -> None:
        scene = Scenario()
        scene.component("svc", path="svc", provider="leaf")
        scene.file("svc/main.py", "x\n")
        scene.file("payload", "ordinary fixture data\n")
        scene.commit()
        self.scene = scene

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def cat_file(self, ref: str) -> bytes:
        result = subprocess.run(
            ["git", "cat-file", "blob", ref], cwd=self.scene.root,
            capture_output=True,
        )
        return result.stdout if result.returncode == 0 else b""


class ConfigContainmentTests(unittest.TestCase):
    """OBL-GIT-SOURCE-006: a config must be inside the source it is read from."""

    def test_an_absolute_path_inside_the_repository_is_accepted(self):
        """The premise: absolute is not itself the objection."""
        with _Repository() as repo:
            absolute = str(repo.scene.root / "boundary.config.json")
            for source in ("head", "index", "working-tree"):
                with self.subTest(source=source):
                    result = run_cli(
                        repo.scene.root, "generate", "--source", source,
                        "--config", absolute,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_absolute_path_outside_the_repository_is_refused(self):
        with _Repository() as repo:
            outside = str(repo.scene.root.parent / "elsewhere.json")
            result = run_cli(
                repo.scene.root, "generate", "--source", "head", "--config", outside
            )
            self.assertEqual(result.returncode, COULD_NOT_CHECK)
            self.assertIn(
                "Source-backed path must stay within the repository", result.stderr
            )

    def test_a_retyped_config_entry_is_refused_by_mode_and_type(self):
        for mode, object_type in (("120000", "blob"), ("160000", "commit")):
            with self.subTest(mode=mode):
                with _Repository() as repo:
                    scene = repo.scene
                    oid = (
                        "1" * 40 if mode == "160000"
                        else scene.git("rev-parse", "HEAD:boundary.config.json")
                    )
                    scene.git("rm", "--cached", "boundary.config.json")
                    scene.git(
                        "update-index", "--add", "--cacheinfo",
                        f"{mode},{oid},boundary.config.json",
                    )
                    scene.git("commit", "-m", "retyped config")
                    result = run_cli(scene.root, "generate", "--source", "head")
                    self.assertEqual(result.returncode, COULD_NOT_CHECK)
                    self.assertIn(
                        "Config path must be a regular file in captured head source",
                        result.stderr,
                    )
                    self.assertIn(f"mode={mode}", result.stderr)
                    self.assertIn(f"type={object_type}", result.stderr)

    def test_an_executable_config_is_allowed(self):
        """The contrast: 100755 is one of the two file modes, not an oddity."""
        with _Repository() as repo:
            scene = repo.scene
            oid = scene.git("rev-parse", "HEAD:boundary.config.json")
            scene.git("rm", "--cached", "boundary.config.json")
            scene.git(
                "update-index", "--add", "--cacheinfo",
                f"100755,{oid},boundary.config.json",
            )
            scene.git("commit", "-m", "executable config")
            self.assertEqual(
                run_cli(scene.root, "generate", "--source", "head").returncode, 0
            )


class RevisionExpressionTests(unittest.TestCase):
    """OBL-GIT-SOURCE-020: a filename must not be read as a revspec."""

    def _reference_for(self, repo, rel: str, source: str) -> str:
        """The immutable object ID _read_path_content hands to cat-file."""
        seen = []

        def capture(repo_root, ref, **kwargs):
            seen.append(ref)
            return b""

        with patch.object(hashing, "_git_cat_blob", capture):
            hashing._read_path_content(
                repo.scene.root, repo.scene.root / rel, source
            )
        self.assertEqual(len(seen), 1, seen)
        return seen[0]

    def test_an_ordinary_path_becomes_an_ordinary_reference(self):
        """The premise: this is how the reference is built."""
        with _Repository() as repo:
            expected = repo.scene.git("rev-parse", "HEAD:payload")
            self.assertEqual(self._reference_for(repo, "payload", "index"), expected)
            self.assertEqual(self._reference_for(repo, "payload", "head"), expected)

    def test_the_reference_grammar_reads_a_stage_prefix(self):
        """The mechanism: ':0:payload' is stage zero of 'payload', not a path."""
        with _Repository() as repo:
            self.assertEqual(
                repo.cat_file(":0:payload"), b"ordinary fixture data\n"
            )
            self.assertEqual(repo.cat_file(":payload"), b"ordinary fixture data\n")

    def test_a_colon_bearing_name_can_resolve_only_to_its_captured_object(self):
        """A path-like revision expression is never passed to cat-file."""
        with _Repository() as repo:
            seen = self._reference_for(repo, "payload", "index")
            self.assertRegex(seen, r"^[0-9a-f]{40,64}$")
            self.assertNotEqual(seen, f":{AMBIGUOUS}")

    def test_git_revision_grammar_remains_ambiguous_but_is_not_used_for_reads(self):
        """The underlying Git grammar still demonstrates why OIDs are used.

        `:0:payload` is stage zero of `payload`, while `HEAD:0:payload` has no
        such reading. Neither expression is used for content reads: boundver
        resolves the captured tree entry and passes its object ID instead.
        """
        with _Repository() as repo:
            self.assertEqual(
                repo.cat_file(f":{AMBIGUOUS}"), b"ordinary fixture data\n"
            )
            self.assertEqual(repo.cat_file(f"HEAD:{AMBIGUOUS}"), b"")
            self.assertEqual(repo.cat_file(":svc/main.py"), b"x\n")

    def test_this_host_will_not_index_such_a_name(self):
        """The bound on reachability, which is the host's and not boundver's."""
        with _Repository() as repo:
            blob = subprocess.run(
                ["git", "hash-object", "-w", "--stdin"], cwd=repo.scene.root,
                input=b"decoy\n", capture_output=True,
            ).stdout.decode().strip()
            added = subprocess.run(
                ["git", "update-index", "--add", "--cacheinfo",
                 f"100644,{blob},{AMBIGUOUS}"],
                cwd=repo.scene.root, capture_output=True, text=True,
            )
            if added.returncode == 0:
                self.skipTest("this host's Git accepts a colon in a path")
            self.assertIn("Invalid path", added.stderr)


class WorktreeDiffRefusalTests(unittest.TestCase):
    """A diff that touches the working tree can run a clean filter.

    Git runs a repository's configured clean filters when it compares the
    working tree, and a clean filter is an external command. So a diff is
    only allowed when it stays out of the working tree: either --cached,
    or two revisions compared against each other. One revision is exactly
    the dangerous case, and the boundary between one and two was asserted
    by nothing - loosening it to allow a single revision left every test
    green (MUT-GIT-SOURCE-214).
    """

    def _refused(self, args):
        with self.assertRaises(ValueError) as caught:
            git._offline_git_command(Path("."), args)
        return str(caught.exception)

    def test_a_single_revision_worktree_diff_is_refused(self):
        for args in (
            ["diff", "HEAD"],
            ["diff", "HEAD", "--", "svc"],
            ["diff", "--name-status", "HEAD"],
        ):
            with self.subTest(args=args):
                self.assertIn(
                    "clean filters can execute external commands",
                    self._refused(args),
                )

    def test_a_bare_worktree_diff_is_refused(self):
        self.assertIn(
            "clean filters can execute external commands",
            self._refused(["diff"]),
        )

    def test_two_revisions_are_allowed(self):
        """The contrast: the refusal is about reaching the working tree."""
        built = git._offline_git_command(Path("."), ["diff", "HEAD~1", "HEAD"])
        self.assertIn("--ignore-submodules=dirty", built)

    def test_cached_is_allowed_with_one_revision(self):
        built = git._offline_git_command(Path("."), ["diff", "--cached", "HEAD"])
        self.assertIn("--cached", built)


class CachedFilterOverrideTests(unittest.TestCase):
    """OBL-GIT-SOURCE-021: a cached answer about executable config."""

    def setUp(self):
        git._repository_filter_config_overrides.cache_clear()

    def tearDown(self):
        git._repository_filter_config_overrides.cache_clear()

    def _overrides(self, repo) -> tuple:
        return git._repository_filter_config_overrides(str(repo.scene.root.resolve()))

    def test_a_driver_present_from_the_start_is_neutralised(self):
        """The premise: the function does find and override a driver."""
        with _Repository() as repo:
            repo.scene.git("config", "filter.evil.clean", "cat")
            names = dict(self._overrides(repo))
            self.assertEqual(names.get("filter.evil.clean"), "")
            self.assertEqual(names.get("filter.evil.required"), "false")

    def test_a_driver_added_later_is_neutralised_too(self):
        """Every subprocess sees the repository's current filter declarations."""
        with _Repository() as repo:
            self.assertEqual(self._overrides(repo), ())
            repo.scene.git("config", "filter.evil.clean", "cat")
            self.assertNotEqual(self._overrides(repo), ())

    def test_cache_clear_remains_a_compatible_no_op(self):
        with _Repository() as repo:
            self.assertEqual(self._overrides(repo), ())
            repo.scene.git("config", "filter.evil.clean", "cat")
            git._repository_filter_config_overrides.cache_clear()
            names = dict(self._overrides(repo))
            self.assertEqual(names.get("filter.evil.clean"), "")


class DirtyPathFailureTests(unittest.TestCase):
    """OBL-GIT-SOURCE-016: a failed question is not a clean answer."""

    def test_a_modified_component_is_reported_dirty(self):
        """The premise: the function does detect a dirty tree."""
        with _Repository() as repo:
            repo.scene.append_line("svc/main.py", "y\n")
            self.assertEqual(
                dirty_component_paths(repo.scene.root, ["svc"]), ["svc"]
            )

    def test_a_clean_component_is_reported_clean(self):
        with _Repository() as repo:
            self.assertEqual(dirty_component_paths(repo.scene.root, ["svc"]), [])

    def test_an_unborn_repository_reports_every_path_dirty(self):
        """The clause that is handled: no HEAD means nothing is known clean."""
        scene = Scenario()
        try:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/main.py", "x\n")
            self.assertEqual(
                dirty_component_paths(scene.root, ["svc", "other"]),
                ["other", "svc"],
            )
        finally:
            scene.close()

    def test_a_git_failure_is_not_reported_as_clean(self):
        """A failed status query has no clean-tree interpretation."""
        def explodes(*args, **kwargs):
            raise subprocess.CalledProcessError(128, ["git", "diff"])

        with _Repository() as repo:
            repo.scene.append_line("svc/main.py", "y\n")
            with patch.object(git, "_git_name_status", explodes):
                with self.assertRaises(ValueError):
                    dirty_component_paths(repo.scene.root, ["svc"])

    def test_the_failure_does_not_replace_the_observed_dirty_answer(self):
        def explodes(*args, **kwargs):
            raise subprocess.CalledProcessError(128, ["git", "diff"])

        with _Repository() as repo:
            repo.scene.append_line("svc/main.py", "y\n")
            self.assertEqual(dirty_component_paths(repo.scene.root, ["svc"]), ["svc"])
            with patch.object(git, "_git_name_status", explodes):
                with self.assertRaises(ValueError):
                    dirty_component_paths(repo.scene.root, ["svc"])


if __name__ == "__main__":
    unittest.main()
