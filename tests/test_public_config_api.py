"""Public ``boundver.load_config`` validation and exception contracts."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import boundver
from tests._repo_fixtures import commit_all, init_git_repo


def _valid_config() -> dict:
    return {
        "project": "public-config-api",
        "components": {
            "svc": {
                "path": "svc",
                "boundary": {
                    "provider": "implicit",
                    "paths": ["api.json"],
                },
            }
        },
        "slices": {},
    }


def _initialize_repo(root: Path, config: Optional[dict] = None) -> dict:
    init_git_repo(root)
    component = root / "svc"
    component.mkdir()
    (component / "api.json").write_text('{"version": 1}\n', encoding="utf-8")
    selected = config if config is not None else _valid_config()
    (root / "boundary.config.json").write_text(
        json.dumps(selected) + "\n",
        encoding="utf-8",
    )
    commit_all(root, "public config fixture")
    return selected


class PublicLoadConfigTests(unittest.TestCase):
    def _load(self, root: Path, *args, **kwargs) -> dict:
        with patch("boundver._git.git_root", return_value=root):
            return boundver.load_config(*args, **kwargs)

    def test_valid_schema_less_config_is_returned(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            expected = _initialize_repo(root)

            loaded = self._load(root)

            self.assertEqual(loaded, expected)
            self.assertNotIn("$schema", loaded)

    def test_lockfile_passed_as_config_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _initialize_repo(root)
            lock_path = root / "not-a-config.json"
            lock_path.write_text(
                json.dumps(
                    {
                        "schema": "boundary-lock/v3",
                        "project": "wrong-document-kind",
                        "components": {},
                        "slices": {},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(boundver.ConfigError) as raised:
                self._load(root, str(lock_path))

            self.assertIsInstance(raised.exception, ValueError)
            message = str(raised.exception)
            self.assertIn("Config is invalid", message)
            self.assertIn("schema", message)
            self.assertIn("at least one component", message)

    def test_semantically_invalid_config_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _initialize_repo(
                root,
                {"project": "invalid", "components": {}, "slices": {}},
            )

            with self.assertRaisesRegex(
                boundver.ConfigError,
                "at least one component",
            ):
                self._load(root)

    def test_malformed_config_raises_exported_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _initialize_repo(root)
            (root / "boundary.config.json").write_text("{not json", encoding="utf-8")

            with self.assertRaises(boundver.ConfigError) as raised:
                self._load(root)

            self.assertIn("JSON parse error", str(raised.exception))

    def test_missing_config_raises_exported_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _initialize_repo(root)

            with self.assertRaises(boundver.ConfigError) as raised:
                self._load(root, "missing.json")

            self.assertNotIsInstance(raised.exception, FileNotFoundError)
            self.assertIn("Config file not found", str(raised.exception))

    def test_missing_declared_filesystem_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = _valid_config()
            config["components"]["svc"]["path"] = "missing"
            _initialize_repo(root, config)

            with self.assertRaisesRegex(
                boundver.ConfigError,
                "path not found or not a directory",
            ):
                self._load(root)

    def test_head_and_index_use_captured_config_instead_of_disk(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            expected = _initialize_repo(root)
            (root / "boundary.config.json").write_text(
                json.dumps({"schema": "boundary-lock/v3"}),
                encoding="utf-8",
            )

            with self.assertRaises(boundver.ConfigError):
                self._load(root, source="working-tree")
            self.assertEqual(self._load(root, source="head"), expected)
            self.assertEqual(self._load(root, source="index"), expected)

    def test_unknown_source_is_a_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _initialize_repo(root)

            with self.assertRaisesRegex(boundver.ConfigError, "Unknown source mode"):
                self._load(root, source="future")

    def test_unreadable_repository_is_a_config_error(self) -> None:
        failure = subprocess.CalledProcessError(128, ["git", "rev-parse"])
        with patch("boundver._git.git_root", side_effect=failure):
            with self.assertRaisesRegex(
                boundver.ConfigError,
                "not inside a readable Git repository",
            ):
                boundver.load_config()

    def test_custom_provider_declarations_are_never_imported(self) -> None:
        config = _valid_config()
        config["providers"] = [
            {
                "module": "untrusted_provider_module",
                "class": "Provider",
                "name": "custom.example",
            }
        ]
        config["components"]["svc"]["boundary"]["provider"] = "custom.example"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _initialize_repo(root, config)

            import importlib

            real_import_module = importlib.import_module

            def guarded_import(name, *args, **kwargs):
                if name == "untrusted_provider_module":
                    raise AssertionError("custom provider code executed")
                return real_import_module(name, *args, **kwargs)

            with patch(
                "boundver.providers.importlib.import_module",
                side_effect=guarded_import,
            ) as importer:
                loaded = self._load(root)

            imported_names = [call.args[0] for call in importer.call_args_list]
            self.assertNotIn("untrusted_provider_module", imported_names)
            self.assertEqual(loaded, config)

    def test_generate_verify_and_load_share_semantic_validation(self) -> None:
        invalid = {"project": "invalid", "components": {}, "slices": {}}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _initialize_repo(root, invalid)
            calls = (
                lambda: boundver.load_config(),
                lambda: boundver.generate(
                    source="working-tree",
                    out_path=None,
                ),
                lambda: boundver.verify(source="working-tree"),
            )

            for call in calls:
                with self.subTest(api=call):
                    with patch("boundver._git.git_root", return_value=root):
                        with self.assertRaisesRegex(
                            boundver.ConfigError,
                            "at least one component",
                        ):
                            call()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
