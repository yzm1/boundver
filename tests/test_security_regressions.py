"""Regressions for fail-closed validation, selection, and source handling."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from boundver._config import validate_config
from boundver._git import changed_components_since_ref
from boundver._lockfile import generate_lockfile
from boundver.providers import PathHashProvider, ProviderContext, compute_boundary
from tests._repo_fixtures import init_git_repo as _init_repo


class ChangedFromRootComponentTests(unittest.TestCase):
    def test_root_component_is_selected_for_root_file_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            readme = root / "README.md"
            readme.write_text("before\n")
            subprocess.run(
                ["git", "add", "README.md"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "baseline"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            readme.write_text("after\n")
            config = {
                "project": "root-project",
                "components": {
                    "root": {
                        "path": ".",
                        "boundary": {"provider": "implicit"},
                    }
                },
                "slices": {},
            }

            selected = changed_components_since_ref(config, root, "HEAD")

            self.assertEqual(selected, ["root"])


class SchemaIndependentConfigValidationTests(unittest.TestCase):
    @staticmethod
    def _valid_config(root: Path) -> dict:
        (root / "svc").mkdir(exist_ok=True)
        return {
            "project": "project",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "leaf"},
                }
            },
            "slices": {},
        }

    def test_project_must_be_a_non_empty_string_without_schema_engine(self):
        with tempfile.TemporaryDirectory() as td, patch(
            "boundver._config._schema_engine_errors", return_value=[]
        ):
            root = Path(td)
            for project in (None, "", "   ", 7, [], {}):
                with self.subTest(project=project):
                    config = self._valid_config(root)
                    config["project"] = project
                    errors = validate_config(config, root)
                    self.assertIn(
                        "Field 'project' must be a non-empty string", errors
                    )

    def test_component_and_slice_keys_must_be_strings_without_schema_engine(self):
        with tempfile.TemporaryDirectory() as td, patch(
            "boundver._config._schema_engine_errors", return_value=[]
        ):
            root = Path(td)
            for invalid_name in ("", 7):
                with self.subTest(kind="component", name=invalid_name):
                    config = self._valid_config(root)
                    component = config["components"].pop("svc")
                    config["components"][invalid_name] = component
                    errors = validate_config(config, root)
                    if isinstance(invalid_name, str):
                        self.assertIn(
                            "Component names must be non-empty strings", errors
                        )
                    else:
                        self.assertTrue(
                            any("non-string mapping key" in error for error in errors),
                            errors,
                        )

                with self.subTest(kind="slice", name=invalid_name):
                    config = self._valid_config(root)
                    config["slices"] = {
                        invalid_name: {
                            "mode": "exact",
                            "components": ["svc"],
                        }
                    }
                    errors = validate_config(config, root)
                    if isinstance(invalid_name, str):
                        self.assertIn("Slice names must be non-empty strings", errors)
                    else:
                        self.assertTrue(
                            any("non-string mapping key" in error for error in errors),
                            errors,
                        )


class SourceAwareValidationTests(unittest.TestCase):
    def test_head_uses_committed_files_deleted_from_working_tree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "boundary.json").write_text('{"contract": 1}\n')
            (root / "svc" / "version.json").write_text(
                '{"version": "1.2.3"}\n'
            )
            config = {
                "project": "source-aware",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {
                            "provider": "json-file",
                            "paths": ["boundary.json"],
                        },
                        "version_source": {
                            "file": "version.json",
                            "field": "version",
                        },
                    }
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(config) + "\n")
            subprocess.run(
                ["git", "add", "."], cwd=root, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", "baseline"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            shutil.rmtree(root / "svc")

            head_errors = validate_config(config, root, source="head")
            head_lock = generate_lockfile(config, root, source="head")
            working_tree_errors = validate_config(
                config, root, source="working-tree"
            )

            self.assertEqual(head_errors, [])
            self.assertEqual(head_lock["components"]["svc"]["version"], "1.2.3")
            self.assertIsNotNone(
                head_lock["components"]["svc"]["fingerprints"]["exact"]
            )
            self.assertIsNotNone(
                head_lock["components"]["svc"]["fingerprints"]["boundary"]
            )
            self.assertTrue(
                any("path not found or not a directory" in error for error in working_tree_errors),
                working_tree_errors,
            )
            self.assertTrue(
                any("version_source.file not found" in error for error in working_tree_errors),
                working_tree_errors,
            )


class RepositoryContainmentValidationTests(unittest.TestCase):
    @staticmethod
    def _config(component: dict) -> dict:
        return {
            "project": "containment",
            "components": {"svc": component},
            "slices": {},
        }

    @staticmethod
    def _symlink_or_skip(
        test_case: unittest.TestCase,
        link: Path,
        target: Path,
        *,
        target_is_directory: bool,
    ) -> None:
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except (OSError, NotImplementedError) as exc:
            test_case.skipTest(f"symlinks are unavailable: {exc}")

    def test_component_root_symlink_escaping_repository_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "repo"
            outside = base / "outside-component"
            root.mkdir()
            outside.mkdir()
            self._symlink_or_skip(
                self,
                root / "svc",
                outside,
                target_is_directory=True,
            )
            config = self._config(
                {
                    "path": "svc",
                    "boundary": {"provider": "leaf"},
                }
            )

            errors = validate_config(config, root, source="working-tree")

            self.assertTrue(
                any("path must not be a symlink: svc" in error for error in errors),
                errors,
            )

    def test_version_source_symlink_escaping_repository_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "repo"
            component = root / "svc"
            component.mkdir(parents=True)
            outside_version = base / "outside-version.json"
            outside_version.write_text('{"version": "1.2.3"}\n')
            self._symlink_or_skip(
                self,
                component / "version.json",
                outside_version,
                target_is_directory=False,
            )
            config = self._config(
                {
                    "path": "svc",
                    "boundary": {"provider": "leaf"},
                    "version_source": {
                        "file": "version.json",
                        "field": "version",
                    },
                }
            )

            errors = validate_config(config, root, source="working-tree")

            self.assertTrue(
                any(
                    "version_source.file must not be a symlink" in error
                    for error in errors
                ),
                errors,
            )

    def test_vendored_copy_parent_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "repo"
            (root / "svc").mkdir(parents=True)
            (base / "outside-vendor").mkdir()
            config = self._config(
                {
                    "path": "svc",
                    "boundary": {"provider": "leaf"},
                    "vendored_copies": ["../outside-vendor"],
                }
            )

            errors = validate_config(config, root, source="working-tree")

            self.assertTrue(
                any(
                    "vendored copy must be a safe repo-relative path" in error
                    for error in errors
                ),
                errors,
            )

    def test_vendored_copy_symlink_escaping_repository_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "repo"
            (root / "svc").mkdir(parents=True)
            outside = base / "outside-vendor"
            outside.mkdir()
            self._symlink_or_skip(
                self,
                root / "vendored-svc",
                outside,
                target_is_directory=True,
            )
            config = self._config(
                {
                    "path": "svc",
                    "boundary": {"provider": "leaf"},
                    "vendored_copies": ["vendored-svc"],
                }
            )

            errors = validate_config(config, root, source="working-tree")

            self.assertTrue(
                any(
                    "vendored copy must be a safe repo-relative path" in error
                    for error in errors
                ),
                errors,
            )


class RootPathBoundaryIdentityTests(unittest.TestCase):
    @staticmethod
    def _context(filename: str) -> ProviderContext:
        files = {filename: b"same contract bytes\n"}
        return ProviderContext(
            repo_root=Path("/repo"),
            component_path=".",
            boundary_cfg={"paths": [filename]},
            source="working-tree",
            read_file=lambda path: files[path],
            list_files=lambda prefix: sorted(
                path
                for path in files
                if path == prefix or path.startswith(prefix + "/")
            ),
        )

    def test_root_file_label_preserves_filename_and_rename_changes_digest(self):
        provider = PathHashProvider()
        before_context = self._context("old-name.json")
        after_context = self._context("new-name.json")

        before_entries = provider.resolve(before_context).entries
        after_entries = provider.resolve(after_context).entries
        before_digest, before_status, before_errors = compute_boundary(
            provider, before_context
        )
        after_digest, after_status, after_errors = compute_boundary(
            provider, after_context
        )

        self.assertEqual(before_entries[0][0], "file:old-name.json")
        self.assertEqual(after_entries[0][0], "file:new-name.json")
        self.assertEqual((before_status, before_errors), ("ok", []))
        self.assertEqual((after_status, after_errors), ("ok", []))
        self.assertNotEqual(before_digest, after_digest)


if __name__ == "__main__":
    unittest.main()
