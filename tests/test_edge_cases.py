"""Tests for uncovered edge cases across _git, _config, providers, versions, core."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from boundver import _config, _lockfile
from boundver._git import (
    _git_batch_cat,
    _load_gitignore_patterns,
    _GitignoreRules,
    _list_files_for_source,
    list_head_files,
    git_latest_tag,
    changed_components_since_ref,
)
from boundver._utils import GuardrailError
from tests._repo_fixtures import init_git_repo as _init_git_repo


# ---------------------------------------------------------------------------
# _git.py edge cases
# ---------------------------------------------------------------------------


class GitBatchCatEdgeCases(unittest.TestCase):
    """Edge cases in _git_batch_cat."""

    def test_ref_with_newline_raises(self):
        """Refs containing newline characters are rejected."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            with self.assertRaises(ValueError) as cm:
                _git_batch_cat(root, ["HEAD:some\nfile"])
            self.assertIn("newline", str(cm.exception))

    def test_ref_with_carriage_return_raises(self):
        """Refs containing carriage return are rejected."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            with self.assertRaises(ValueError) as cm:
                _git_batch_cat(root, ["HEAD:some\rfile"])
            self.assertIn("newline", str(cm.exception))

    def test_missing_object_raises(self):
        """Non-existent paths fail closed."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "x.txt").write_text("hello\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            with self.assertRaisesRegex(ValueError, "not found"):
                _git_batch_cat(root, ["HEAD:nonexistent.txt"])

    def test_empty_refs_returns_empty_dict(self):
        """Empty refs list returns empty dict immediately."""
        with tempfile.TemporaryDirectory() as td:
            result = _git_batch_cat(Path(td), [])
            self.assertEqual(result, {})


class GitignoreTests(unittest.TestCase):
    """Tests for gitignore parsing and matching."""

    def test_no_gitignore_returns_none(self):
        """Repo without .gitignore returns None."""
        with tempfile.TemporaryDirectory() as td:
            result = _load_gitignore_patterns(Path(td))
            self.assertIsNone(result)

    def test_gitignore_rules_simple_pattern(self):
        """Simple patterns match any path component."""
        rules = _GitignoreRules()
        rules.add("*.pyc")
        self.assertTrue(rules.is_ignored("src/foo.pyc"))
        self.assertTrue(rules.is_ignored("foo.pyc"))
        self.assertFalse(rules.is_ignored("foo.py"))

    def test_gitignore_leading_slash_anchors_slashless_pattern(self):
        rules = _GitignoreRules()
        rules.add("/foo")

        self.assertTrue(rules.is_ignored("foo"))
        self.assertTrue(rules.is_ignored("foo/contract.json"))
        self.assertFalse(rules.is_ignored("nested/foo"))
        self.assertFalse(rules.is_ignored("nested/foo/contract.json"))

    def test_gitignore_rules_directory_pattern(self):
        """Patterns with / match from root."""
        rules = _GitignoreRules()
        rules.add("dist")
        self.assertTrue(rules.is_ignored("dist/bundle.js"))
        self.assertTrue(rules.is_ignored("dist"))

    def test_gitignore_rules_doublestar(self):
        """** glob patterns match recursively."""
        rules = _GitignoreRules()
        rules.add("**/build")
        self.assertTrue(rules.is_ignored("build"))
        self.assertTrue(rules.is_ignored("src/build"))
        self.assertTrue(rules.is_ignored("a/b/build"))
        self.assertFalse(rules.is_ignored("buildx"))

    def test_gitignore_rules_negation(self):
        """! patterns negate previous matches."""
        rules = _GitignoreRules()
        rules.add("*.log")
        rules.add("!important.log")
        self.assertTrue(rules.is_ignored("debug.log"))
        self.assertFalse(rules.is_ignored("important.log"))

    def test_gitignore_rules_path_pattern(self):
        """Patterns with / match as path prefix."""
        rules = _GitignoreRules()
        rules.add("docs/internal")
        self.assertTrue(rules.is_ignored("docs/internal"))
        self.assertTrue(rules.is_ignored("docs/internal/secret.md"))
        self.assertFalse(rules.is_ignored("other/docs/internal"))

    def test_gitignore_doublestar_middle(self):
        """foo/**/bar matches foo/bar, foo/x/bar, foo/x/y/bar."""
        rules = _GitignoreRules()
        rules.add("foo/**/bar")
        self.assertTrue(rules.is_ignored("foo/bar"))
        self.assertTrue(rules.is_ignored("foo/x/bar"))
        self.assertTrue(rules.is_ignored("foo/x/y/bar"))
        self.assertFalse(rules.is_ignored("baz/foo/bar"))

    def test_unborn_listing_uses_installed_gitignore_semantics(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / ".gitignore").write_text("a**/b\n", encoding="utf-8")
            for relative in ("ax/b", "a/x/b"):
                candidate = root / relative
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_text("contract\n", encoding="utf-8")

            git_results = {
                relative: subprocess.run(
                    ["git", "check-ignore", "-v", "--no-index", "--", relative],
                    cwd=root,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                for relative in ("ax/b", "a/x/b")
            }
            files = set(_list_files_for_source(root, ".", "working-tree"))

        self.assertEqual(git_results["ax/b"].returncode, 0)
        for relative, result in git_results.items():
            with self.subTest(relative=relative):
                self.assertEqual(relative not in files, result.returncode == 0)

    def test_gitignore_trailing_doublestar_requires_a_descendant(self):
        rules = _GitignoreRules()
        rules.add("src/**")

        self.assertFalse(rules.is_ignored("src"))
        self.assertTrue(rules.is_ignored("src/module.py"))
        self.assertTrue(rules.is_ignored("src/package/module.py"))

    def test_gitignore_many_middle_doublestars_have_bounded_work(self):
        rules = _GitignoreRules()
        pattern = "a" + "/**/a" * 11 + "/z"
        candidate = "/".join(["a"] * 33) + "/y"
        rules.add(pattern)

        self.assertFalse(rules.is_ignored(candidate))
        self.assertLess(rules._match_steps, 10_000)


class ListFilesForSourceTests(unittest.TestCase):
    """Tests for _list_files_for_source filesystem fallback."""

    def test_filesystem_fallback_with_gitignore(self):
        """Falls back to filesystem with gitignore filtering when not a git repo."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".gitignore").write_text("*.log\nbuild/\n")
            sub = root / "svc"
            sub.mkdir()
            (sub / "main.py").write_text("x=1\n")
            (sub / "debug.log").write_text("log\n")
            (sub / "build").mkdir()
            (sub / "build" / "out.js").write_text("out\n")
            result = _list_files_for_source(root, "svc", "working-tree")
            self.assertIn("svc/main.py", result)
            self.assertNotIn("svc/debug.log", result)
            self.assertNotIn("svc/build/out.js", result)

    def test_unborn_repository_fallback_bounds_adversarial_gitignore(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pattern = "a" + "/**/a" * 11 + "/z"
            (root / ".gitignore").write_text(pattern + "\n", encoding="utf-8")
            directory = root
            for _ in range(33):
                directory /= "a"
                directory.mkdir()
            candidate = directory / "y"
            candidate.write_text("contract\n", encoding="utf-8")

            files = _list_files_for_source(root, "a", "working-tree")

            self.assertIn(candidate.relative_to(root).as_posix(), files)

    def test_unborn_fallback_trailing_doublestar_keeps_same_named_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / ".gitignore").write_text("src/**\n", encoding="utf-8")
            (root / "src").write_text("contract\n", encoding="utf-8")

            files = _list_files_for_source(root, "src", "working-tree")

            self.assertIn("src", files)

    def test_unborn_fallback_preserves_root_anchored_ignore_rule(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / ".gitignore").write_text("/foo\n", encoding="utf-8")
            nested = root / "nested" / "foo"
            nested.mkdir(parents=True)
            contract = nested / "contract.json"
            contract.write_text("{}\n", encoding="utf-8")

            files = _list_files_for_source(root, "nested", "working-tree")

            self.assertIn("nested/foo/contract.json", files)

    def test_gitignore_rule_count_fails_closed(self):
        rules = _GitignoreRules()
        with patch("boundver._git.MAX_GITIGNORE_RULES", 1):
            rules.add("first")
            with self.assertRaisesRegex(GuardrailError, "more than 1 rules"):
                rules.add("second")

    def test_gitignore_aggregate_match_budget_fails_closed(self):
        rules = _GitignoreRules()
        rules.add("*.log")
        with patch("boundver._git.MAX_GITIGNORE_MATCH_STEPS", 3):
            with self.assertRaisesRegex(GuardrailError, "aggregate matcher steps"):
                rules.is_ignored("nested/debug.log")

    def test_filesystem_fallback_single_file(self):
        """Falls back to single file when path is a file."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "file.txt").write_text("hi\n")
            result = _list_files_for_source(root, "file.txt", "working-tree")
            self.assertEqual(result, ["file.txt"])

    def test_filesystem_fallback_nonexistent(self):
        """Returns empty list when path doesn't exist."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = _list_files_for_source(root, "nope", "working-tree")
            self.assertEqual(result, [])

    def test_index_source_appends_cached(self):
        """source='index' uses --cached flag."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "f.txt").write_text("hi\n")
            subprocess.run(["git", "add", "f.txt"], cwd=root, check=True, capture_output=True)
            result = _list_files_for_source(root, "f.txt", "index")
            self.assertIn("f.txt", result)


class ListHeadFilesTests(unittest.TestCase):
    """Tests for list_head_files edge cases."""

    def test_empty_repo_returns_empty(self):
        """Repo with no commits returns empty list."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            result = list_head_files(root, "anything")
            self.assertEqual(result, [])

    def test_single_file_path(self):
        """Single file ref returns just that file."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "single.txt").write_text("data\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            result = list_head_files(root, "single.txt")
            self.assertEqual(result, ["single.txt"])


class GitLatestTagTests(unittest.TestCase):
    """Tests for git_latest_tag."""

    def test_no_tags_returns_none(self):
        """Repo with no tags returns None."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "f.txt").write_text("x\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            result = git_latest_tag(root, "v")
            self.assertIsNone(result)

    def test_matching_tag_returns_version(self):
        """Tag matching prefix returns version part."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "f.txt").write_text("x\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "tag", "v1.2.3"], cwd=root, check=True, capture_output=True)
            result = git_latest_tag(root, "v")
            self.assertEqual(result, "1.2.3")

    def test_prefix_only_tag_returns_none(self):
        """Tag that matches prefix exactly (empty version part) returns None."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "f.txt").write_text("x\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "tag", "release-"], cwd=root, check=True, capture_output=True)
            result = git_latest_tag(root, "release-")
            self.assertIsNone(result)

    def test_tag_on_ancestor_commit_is_reachable(self):
        """A tag on a HEAD ancestor is selected by the reachable-tag query."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "f.txt").write_text("x\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "tag", "svc-v2.0.0"], cwd=root, check=True, capture_output=True)
            # The new branch retains the tagged commit as an ancestor.
            subprocess.run(["git", "checkout", "-b", "other"], cwd=root, check=True, capture_output=True)
            (root / "f.txt").write_text("y\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "other"], cwd=root, check=True, capture_output=True)
            result = git_latest_tag(root, "svc-v")
            self.assertEqual(result, "2.0.0")


class ChangedComponentsSinceRefTests(unittest.TestCase):
    """Tests for changed_components_since_ref."""

    def test_invalid_ref_raises(self):
        """A non-existent ref fails closed instead of selecting no components."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "f.txt").write_text("x\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            config = {"components": {"svc": {"path": "svc"}}}
            with self.assertRaisesRegex(
                ValueError, "Cannot resolve changed-from Git ref"
            ):
                changed_components_since_ref(config, root, "nonexistent_ref_xyz")

    def test_detects_changed_component(self):
        """Correctly identifies component with changed files."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            (root / "other").mkdir()
            (root / "other" / "lib.py").write_text("y=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "tag", "baseline"], cwd=root, check=True, capture_output=True)
            (root / "svc" / "main.py").write_text("x=2\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "change"], cwd=root, check=True, capture_output=True)
            config = {
                "components": {
                    "svc": {"path": "svc"},
                    "other": {"path": "other"},
                }
            }
            result = changed_components_since_ref(config, root, "baseline")
            self.assertEqual(result, ["svc"])


# ---------------------------------------------------------------------------
# _config.py validation edge cases
# ---------------------------------------------------------------------------


class ConfigValidationEdgeCases(unittest.TestCase):
    """Tests for validate_config covering uncovered branches."""

    def _base_cfg(self):
        return {
            "project": "p",
            "components": {},
            "slices": {},
        }

    def test_custom_provider_declaration_limit_is_dependency_independent(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self._base_cfg()
            cfg["providers"] = [
                {"module": f"provider_{index}", "class": "Provider"}
                for index in range(3)
            ]
            with patch.object(_config, "MAX_CUSTOM_PROVIDERS", 2):
                errors = _config.validate_config(cfg, Path(td))
        self.assertTrue(any("provider limit" in error for error in errors))

    def test_boundary_path_declaration_limit_is_dependency_independent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = self._base_cfg()
            cfg["components"] = {
                "svc": {
                    "path": "svc",
                    "boundary": {
                        "provider": "path-hash",
                        "paths": ["one", "two", "three"],
                    },
                }
            }
            with patch.object(_config, "MAX_PROVIDER_DECLARATIONS", 2):
                errors = _config.validate_config(cfg, root)
        self.assertTrue(any("declaration limit" in error for error in errors))

    def test_load_config_file_not_found(self):
        """Loading non-existent config raises FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            _config.load_config_file(Path("/nonexistent/boundary.config.json"))

    def test_yaml_config_without_pyyaml(self):
        """YAML config without PyYAML raises a controlled config error."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.yaml"
            p.write_text("project: test\n")
            with patch.dict(sys.modules, {"yaml": None}):
                with patch("builtins.__import__", side_effect=ImportError("no yaml")):
                    with self.assertRaisesRegex(
                        _config.ConfigError, "PyYAML is not installed"
                    ):
                        _config.load_config_file(p)

    def test_providers_not_a_list(self):
        """Top-level providers that isn't a list produces error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = self._base_cfg()
            cfg["providers"] = "invalid"
            errors = _config.validate_config(cfg, root)
            self.assertTrue(any("'providers' must be an array" in e for e in errors))

    def test_providers_entry_not_dict(self):
        """Provider entry that isn't a dict produces error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = self._base_cfg()
            cfg["providers"] = ["not_a_dict"]
            errors = _config.validate_config(cfg, root)
            self.assertTrue(any("must be an object" in e for e in errors))

    def test_providers_entry_missing_module_field(self):
        """Provider entry without module produces error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = self._base_cfg()
            cfg["providers"] = [{"class": "MyClass"}]
            errors = _config.validate_config(cfg, root)
            self.assertTrue(any("missing required string field 'module'" in e for e in errors))

    def test_providers_entry_bad_name_prefix(self):
        """Provider entry with name not starting with custom. produces error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = self._base_cfg()
            cfg["providers"] = [{"module": "my_mod", "class": "Cls", "name": "bad_prefix"}]
            errors = _config.validate_config(cfg, root)
            self.assertTrue(any("must start with 'custom.'" in e for e in errors))

    def test_providers_entry_valid_name_tracked(self):
        """Provider entry with valid custom.* name is accepted."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = self._base_cfg()
            cfg["providers"] = [{"module": "my_mod", "class": "Cls", "name": "custom.mine"}]
            cfg["components"] = {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "custom.mine", "paths": []},
                }
            }
            errors = _config.validate_config(cfg, root)
            # Should NOT have "no 'providers' list" error since we declared providers
            self.assertFalse(any("no 'providers' list" in e for e in errors))
            # Should NOT have "must start with 'custom.'" error
            self.assertFalse(any("must start with 'custom.'" in e for e in errors))

    def test_custom_provider_without_providers_list(self):
        """Using custom.Foo provider without top-level providers list produces error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = self._base_cfg()
            cfg["components"] = {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "custom.MyProv", "paths": []},
                }
            }
            errors = _config.validate_config(cfg, root)
            self.assertTrue(any("no 'providers' list" in e for e in errors))

    def test_version_source_not_dict(self):
        """version_source that isn't a dict produces error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = self._base_cfg()
            cfg["components"] = {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "implicit"},
                    "version_source": "invalid",
                }
            }
            errors = _config.validate_config(cfg, root)
            self.assertTrue(any("must be an object" in e for e in errors))

    def test_version_source_empty_git_tag_prefix(self):
        """Empty git_tag_prefix produces error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = self._base_cfg()
            cfg["components"] = {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "implicit"},
                    "version_source": {"git_tag_prefix": ""},
                }
            }
            errors = _config.validate_config(cfg, root)
            self.assertTrue(any("git_tag_prefix" in e for e in errors))

    def test_version_source_empty_file(self):
        """Empty version_source.file produces error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = self._base_cfg()
            cfg["components"] = {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "implicit"},
                    "version_source": {"file": "", "field": "version"},
                }
            }
            errors = _config.validate_config(cfg, root)
            self.assertTrue(any("file" in e.lower() for e in errors))

    def test_version_source_unsupported_extension(self):
        """Unsupported file extension produces error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = self._base_cfg()
            cfg["components"] = {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "implicit"},
                    "version_source": {"file": "version.txt", "field": "version"},
                }
            }
            errors = _config.validate_config(cfg, root)
            self.assertTrue(any("unsupported extension" in e for e in errors))

    def test_version_source_missing_file_on_disk(self):
        """version_source file not on disk produces error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = self._base_cfg()
            cfg["components"] = {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "implicit"},
                    "version_source": {"file": "package.json", "field": "version"},
                }
            }
            errors = _config.validate_config(cfg, root)
            self.assertTrue(any("not found" in e for e in errors))

    def test_version_source_file_without_field(self):
        """version_source with file but no field produces error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "pyproject.toml").write_text('[project]\nversion = "1.0"\n')
            cfg = self._base_cfg()
            cfg["components"] = {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "implicit"},
                    "version_source": {"file": "pyproject.toml"},
                }
            }
            errors = _config.validate_config(cfg, root)
            self.assertTrue(any("no 'field'" in e for e in errors))

    def test_version_source_neither_file_nor_tag(self):
        """version_source with no file or git_tag_prefix produces error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = self._base_cfg()
            cfg["components"] = {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "implicit"},
                    "version_source": {"something_else": True},
                }
            }
            errors = _config.validate_config(cfg, root)
            self.assertTrue(any("'file' or 'git_tag_prefix'" in e for e in errors))

    def test_slice_not_dict(self):
        """Slice definition that isn't a dict produces error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = self._base_cfg()
            cfg["slices"] = {"bad": "string_not_dict"}
            errors = _config.validate_config(cfg, root)
            self.assertTrue(any("must be an object" in e for e in errors))

    def test_slice_unknown_mode(self):
        """Slice with unknown mode produces error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = self._base_cfg()
            cfg["slices"] = {"s": {"mode": "nonexistent_mode", "components": []}}
            errors = _config.validate_config(cfg, root)
            self.assertTrue(any("unknown mode" in e for e in errors))

    def test_slice_components_not_string_list(self):
        """Slice with non-string components produces error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = self._base_cfg()
            cfg["slices"] = {"s": {"mode": "exact", "components": [123]}}
            errors = _config.validate_config(cfg, root)
            self.assertTrue(any("array of strings" in e for e in errors))

    def test_boundary_path_escapes_component(self):
        """Boundary path that escapes component root produces error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = self._base_cfg()
            cfg["components"] = {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "openapi", "paths": ["../../etc/passwd"]},
                }
            }
            errors = _config.validate_config(cfg, root)
            self.assertTrue(any("escapes" in e for e in errors))

    def test_boundary_path_glob_with_dotdot(self):
        """Glob pattern with .. produces error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = self._base_cfg()
            cfg["components"] = {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "openapi", "paths": ["../**/*.py"]},
                }
            }
            errors = _config.validate_config(cfg, root)
            self.assertTrue(any(".." in e for e in errors))

    def test_boundary_path_not_found(self):
        """Non-existent boundary path produces error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = self._base_cfg()
            cfg["components"] = {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "openapi", "paths": ["missing.yaml"]},
                }
            }
            errors = _config.validate_config(cfg, root)
            self.assertTrue(any("not found" in e for e in errors))


class ConfigWarningsTests(unittest.TestCase):
    """Tests for config_warnings (behavior superset check)."""

    def test_behavior_not_superset_of_boundary_warns(self):
        """Warns when behavior.paths doesn't cover all boundary.paths."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("api\n")
            (root / "svc" / "config.json").write_text("{}\n")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "behavior": {"paths": ["config.json"]},
                    }
                },
                "slices": {},
            }
            warnings = _config.config_warnings(cfg, root)
            self.assertTrue(any("superset" in w for w in warnings))


# ---------------------------------------------------------------------------
# providers.py edge cases
# ---------------------------------------------------------------------------


class CustomProviderLoadingTests(unittest.TestCase):
    """Tests for load_custom_providers edge cases."""

    def test_provider_instantiation_failure(self):
        """Provider class that raises in __init__ produces error."""
        from boundver.providers import load_custom_providers
        # Create a mock module with a failing class
        mock_mod = MagicMock()
        class BadProvider:
            def __init__(self):
                raise RuntimeError("broken init")
        mock_mod.BadProvider = BadProvider
        providers_list = [{"module": "my_custom_mod", "class": "BadProvider"}]
        with patch("importlib.import_module", return_value=mock_mod):
            errors = load_custom_providers(providers_list, allow_custom=True)
        self.assertTrue(any("Failed to instantiate" in e for e in errors))

    def test_provider_bad_name_prefix(self):
        """Provider with name not starting with 'custom.' produces error."""
        from boundver.providers import load_custom_providers
        mock_mod = MagicMock()
        class BadNameProvider:
            name = "not_custom_prefix"
            def resolve(self, ctx):
                pass
        mock_mod.BadNameProvider = BadNameProvider
        providers_list = [{"module": "my_mod", "class": "BadNameProvider"}]
        with patch("importlib.import_module", return_value=mock_mod):
            errors = load_custom_providers(providers_list, allow_custom=True)
        self.assertTrue(any("must start with 'custom.'" in e for e in errors))

    def test_provider_import_failure(self):
        """Module that cannot be imported produces error."""
        from boundver.providers import load_custom_providers
        providers_list = [{"module": "nonexistent_module_xyz", "class": "Foo"}]
        errors = load_custom_providers(providers_list, allow_custom=True)
        self.assertTrue(any("Failed to import" in e for e in errors))

    def test_provider_class_not_found(self):
        """Module exists but class doesn't produces error."""
        from boundver.providers import load_custom_providers
        mock_mod = MagicMock(spec=[])  # spec=[] means no attributes
        providers_list = [{"module": "my_mod", "class": "NonexistentClass"}]
        with patch("importlib.import_module", return_value=mock_mod):
            errors = load_custom_providers(providers_list, allow_custom=True)
        self.assertTrue(any("has no attribute" in e for e in errors))

    def test_provider_not_allowed(self):
        """Custom providers blocked when allow_custom=False."""
        from boundver.providers import load_custom_providers
        providers_list = [{"module": "m", "class": "C"}]
        errors = load_custom_providers(providers_list, allow_custom=False)
        self.assertTrue(any("not enabled" in e for e in errors))

    def test_provider_invalid_module_name(self):
        """Invalid module name (not a valid Python path) produces error."""
        from boundver.providers import load_custom_providers
        providers_list = [{"module": "invalid-module-name!", "class": "Cls"}]
        errors = load_custom_providers(providers_list, allow_custom=True)
        self.assertTrue(any("not a valid Python module path" in e for e in errors))

    def test_provider_invalid_class_name(self):
        """Invalid class name (not a valid Python identifier) produces error."""
        from boundver.providers import load_custom_providers
        providers_list = [{"module": "valid_module", "class": "not-valid-class!"}]
        errors = load_custom_providers(providers_list, allow_custom=True)
        self.assertTrue(any("not a valid Python identifier" in e for e in errors))

    def test_provider_missing_module_field(self):
        """Provider entry without module field produces error."""
        from boundver.providers import load_custom_providers
        providers_list = [{"class": "Foo"}]
        errors = load_custom_providers(providers_list, allow_custom=True)
        self.assertTrue(any("missing required fields" in e for e in errors))


# ---------------------------------------------------------------------------
# versions.py edge cases
# ---------------------------------------------------------------------------


class VersionsParserAvailabilityTests(unittest.TestCase):
    """Missing authoritative version parsers fail closed."""

    def test_toml_without_tomllib_or_tomli_returns_none(self):
        import boundver.versions as v_mod

        documents = (
            ('version = "3.2.1"\n', "version"),
            ('[tool.poetry]\nversion = "0.9.0"\n', "tool.poetry.version"),
            ("version = '1.5.0'\n", "version"),
        )
        with patch.object(v_mod, "tomllib", None):
            for document, field in documents:
                with self.subTest(document=document):
                    self.assertIsNone(
                        v_mod._extract_toml_from_text(document, field)
                    )

    def test_yaml_without_pyyaml_returns_none(self):
        import boundver.versions as v_mod

        documents = (
            ("version: 4.5.6\n", "version"),
            ("info:\n  version: '2.3.4'\n", "info.version"),
            ("version: 5.0.0 # latest\n", "version"),
        )
        with patch.object(v_mod, "yaml", None):
            for document, field in documents:
                with self.subTest(document=document):
                    self.assertIsNone(
                        v_mod._extract_yaml_from_text(document, field)
                    )


# ---------------------------------------------------------------------------
# core.py CLI edge cases
# ---------------------------------------------------------------------------


class CoreCLIDiffEdgeCases(unittest.TestCase):
    """Tests for diff command edge cases."""

    def test_diff_path_traversal_rejected(self):
        """Diff rejects paths containing '..'."""
        from boundver.core import main as core_main
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "boundary.config.json").write_text(json.dumps({
                "project": "p", "components": {}, "slices": {}
            }))
            import io
            from contextlib import redirect_stderr, redirect_stdout
            with self.assertRaises(SystemExit) as cm:
                sys.argv = ["boundver", "diff", "../evil.json", "other.json"]
                with redirect_stderr(io.StringIO()):
                    with redirect_stdout(io.StringIO()):
                        with patch("boundver.core.git_root", return_value=root):
                            core_main()
            self.assertEqual(cm.exception.code, 2)

    def test_diff_missing_file_rejected(self):
        """Diff rejects non-existent lockfile paths."""
        from boundver.core import main as core_main
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "boundary.config.json").write_text(json.dumps({
                "project": "p", "components": {}, "slices": {}
            }))
            import io
            from contextlib import redirect_stderr, redirect_stdout
            with self.assertRaises(SystemExit) as cm:
                sys.argv = ["boundver", "diff", "nonexistent1.json", "nonexistent2.json"]
                with redirect_stderr(io.StringIO()):
                    with redirect_stdout(io.StringIO()):
                        with patch("boundver.core.git_root", return_value=root):
                            core_main()
            self.assertEqual(cm.exception.code, 2)


class LockfileVerifyEdgeCases(unittest.TestCase):
    """Tests for verify_lockfile hidden edge cases."""

    def test_verify_detects_removed_component(self):
        """verify reports component that's in lockfile but not in config."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            (root / "keep").mkdir()
            (root / "keep" / "main.py").write_text("y=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "implicit"}},
                    "keep": {"path": "keep", "boundary": {"provider": "implicit"}},
                },
                "slices": {},
            }
            lockfile = _lockfile.generate_lockfile(cfg, root, source="head")
            # Now verify with config that removed 'svc'
            cfg_without = {
                "project": "p",
                "components": {
                    "keep": {"path": "keep", "boundary": {"provider": "implicit"}},
                },
                "slices": {},
            }
            issues = _lockfile.verify_lockfile(cfg_without, lockfile, root, source="head")
            self.assertTrue(any("REMOVED" in i for i in issues))

    def test_verify_detects_new_component(self):
        """verify reports component in config but missing from lockfile."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            (root / "extra").mkdir()
            (root / "extra" / "lib.py").write_text("y=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            # Generate lockfile with only svc
            cfg_partial = {
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "implicit"}},
                },
                "slices": {},
            }
            lockfile = _lockfile.generate_lockfile(cfg_partial, root, source="head")
            # Verify with config that adds extra
            cfg_full = {
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "implicit"}},
                    "extra": {"path": "extra", "boundary": {"provider": "implicit"}},
                },
                "slices": {},
            }
            issues = _lockfile.verify_lockfile(cfg_full, lockfile, root, source="head")
            self.assertTrue(any("NEW" in i for i in issues))

    def test_verify_detects_malformed_lockfile(self):
        """verify reports malformed lockfile missing required fingerprints."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "implicit"}},
                },
                "slices": {},
            }
            # Manually crafted bad lockfile
            bad_lockfile = {
                "schema": "boundary-lock/v1",
                "project": "p",
                "components": {
                    "svc": {
                        "version": None,
                        "fingerprints": {"exact": "aaa"},  # missing behavior, boundary, compat
                    }
                },
                "slices": {},
            }
            issues = _lockfile.verify_lockfile(cfg, bad_lockfile, root, source="head")
            self.assertTrue(any("malformed" in i.lower() or "missing" in i.lower() for i in issues))


class LockfileVersionReadFileTests(unittest.TestCase):
    """Tests for _version_read_file index source path."""

    def test_generate_with_index_source(self):
        """generate_lockfile with source='index' reads staged content."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            (root / "svc" / "package.json").write_text('{"version": "1.0.0"}\n')
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            # Stage a version bump
            (root / "svc" / "package.json").write_text('{"version": "2.0.0"}\n')
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},
                        "version_source": {"file": "package.json", "field": "version"},
                    },
                },
                "slices": {},
            }
            lockfile = _lockfile.generate_lockfile(cfg, root, source="index")
            self.assertEqual(lockfile["components"]["svc"]["version"], "2.0.0")


# ---------------------------------------------------------------------------
# _output.py edge cases
# ---------------------------------------------------------------------------


class WhyComponentEdgeCases(unittest.TestCase):
    """Tests for why_component with different sources."""

    def test_why_working_tree_shows_changed_files(self):
        """why with source=working-tree shows modified files."""
        from boundver._output import why_component
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "implicit"}},
                },
                "slices": {},
            }
            lockfile = _lockfile.generate_lockfile(cfg, root, source="head")
            # Modify file (uncommitted)
            (root / "svc" / "main.py").write_text("x=2\n")
            rc = why_component(cfg, lockfile, root, "svc", source="working-tree")
            self.assertEqual(rc, 1)

    def test_why_index_source(self):
        """why with source=index detects staged changes."""
        from boundver._output import why_component
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "implicit"}},
                },
                "slices": {},
            }
            lockfile = _lockfile.generate_lockfile(cfg, root, source="head")
            # Stage a change
            (root / "svc" / "main.py").write_text("x=2\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            rc = why_component(cfg, lockfile, root, "svc", source="index")
            self.assertEqual(rc, 1)

    def test_why_index_no_staged_changes_under_component(self):
        """why with source=index shows hint when no staged changes found under component."""
        from boundver._output import why_component
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "implicit"}},
                },
                "slices": {},
            }
            lockfile = _lockfile.generate_lockfile(cfg, root, source="head")
            # Create drift by committing a change (so HEAD differs from lockfile)
            (root / "svc" / "main.py").write_text("x=2\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "change"], cwd=root, check=True, capture_output=True)
            # Now source=index has no staged changes, but lockfile is stale
            rc = why_component(cfg, lockfile, root, "svc", source="index")
            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
