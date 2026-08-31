"""Literal Git tag-prefix validation and source-resolution contracts."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from boundver._config import validate_config
from boundver._config_contract import (
    MAX_GIT_TAG_PREFIX_CHARS,
    git_tag_prefix_error,
)
from boundver._git import git_latest_tag
from boundver._lockfile import generate_lockfile
from boundver._utils import ConfigError
from boundver.versions import extract_version
from tests._repo_fixtures import commit_all, init_git_repo


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def _config(prefix: object) -> dict:
    return {
        "project": "tag-prefix-test",
        "components": {
            "svc": {
                "path": "svc",
                "version_source": {"git_tag_prefix": prefix},
                "boundary": {
                    "provider": "implicit",
                    "paths": ["api.json"],
                },
            }
        },
        "slices": {},
    }


def _initialize_component(root: Path) -> None:
    init_git_repo(root)
    component = root / "svc"
    component.mkdir()
    (component / "api.json").write_text('{"version": 1}\n', encoding="utf-8")
    (component / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    commit_all(root, "initial component")


class GitTagPrefixGrammarTests(unittest.TestCase):
    def test_valid_prefixes_match_git_candidate_rules(self) -> None:
        prefixes = (
            "v",
            "service-v",
            "team/service-v",
            "rélease-v",
            "releases/",
            "@",
            "candidate.",
            "candidate.lock",
        )
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                self.assertIsNone(git_tag_prefix_error(prefix))
                result = subprocess.run(
                    ["git", "check-ref-format", f"refs/tags/{prefix}0.0.0"],
                    check=False,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_git_forbidden_prefixes_are_rejected(self) -> None:
        prefixes = (
            " bad",
            "bad ",
            "bad prefix",
            "bad\t",
            "v*",
            "v?",
            "v[",
            "v\\",
            "v~",
            "v^",
            "v:",
            "v..next",
            "v@{next",
            "/v",
            "team//v",
            ".hidden",
            "team/.hidden",
            "team.lock/v",
        )
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                self.assertIsNotNone(git_tag_prefix_error(prefix))
                result = subprocess.run(
                    ["git", "check-ref-format", f"refs/tags/{prefix}0.0.0"],
                    check=False,
                    capture_output=True,
                )
                self.assertNotEqual(result.returncode, 0)

    def test_type_empty_control_and_length_limits_are_bounded(self) -> None:
        for prefix in (None, True, 1, "", "v\x00", "v\x1f", "v\x7f"):
            with self.subTest(prefix=prefix):
                self.assertIsNotNone(git_tag_prefix_error(prefix))
        error = git_tag_prefix_error("v" * (MAX_GIT_TAG_PREFIX_CHARS + 1))
        self.assertIn(str(MAX_GIT_TAG_PREFIX_CHARS), error or "")

    def test_invalid_prefix_never_reaches_tag_resolver(self) -> None:
        resolver = MagicMock(return_value="1.2.3")
        self.assertIsNone(
            extract_version(
                Path("."),
                "svc",
                {"git_tag_prefix": "v*"},
                resolver,
            )
        )
        resolver.assert_not_called()

    def test_direct_tag_lookup_rejects_invalid_prefix_before_git(self) -> None:
        with patch("boundver._git._git_run") as git_run:
            with self.assertRaisesRegex(ValueError, "Invalid literal Git tag prefix"):
                git_latest_tag(Path("."), "bad prefix")
        git_run.assert_not_called()


class GitTagPrefixConfigAndGenerationTests(unittest.TestCase):
    def test_dependency_free_validation_rejects_invalid_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _initialize_component(root)
            for prefix in ("bad prefix", "v*", "team//v", ".hidden"):
                with self.subTest(prefix=prefix):
                    with patch(
                        "boundver._config._schema_engine_errors", return_value=[]
                    ):
                        errors = validate_config(_config(prefix), root)
                    prefix_errors = [
                        error for error in errors if "git_tag_prefix" in error
                    ]
                    self.assertTrue(prefix_errors, errors)
                    self.assertTrue(
                        any("literal prefix" in error for error in prefix_errors),
                        prefix_errors,
                    )

    def test_public_schema_matches_dependency_free_validation(self) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError:  # pragma: no cover - optional schema extra
            self.skipTest("jsonschema is not installed")
        schema_path = Path(__file__).parents[1] / "boundary.config.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)

        for prefix, valid in (
            ("team/rélease-v", True),
            ("releases/", True),
            ("bad prefix", False),
            ("v*", False),
            ("team//v", False),
            (".hidden", False),
        ):
            with self.subTest(prefix=prefix):
                schema_errors = list(validator.iter_errors(_config(prefix)))
                self.assertEqual(not schema_errors, valid, schema_errors)
                self.assertEqual(git_tag_prefix_error(prefix) is None, valid)

    def test_validation_accepts_unicode_and_namespaced_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _initialize_component(root)
            for prefix in ("rélease-v", "team/service-v", "releases/"):
                with self.subTest(prefix=prefix):
                    errors = validate_config(_config(prefix), root)
                    self.assertFalse(
                        any("git_tag_prefix" in error for error in errors),
                        errors,
                    )

    def test_generation_rejects_invalid_prefix_before_source_capture(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch("boundver._lockfile._capture_git_source_snapshot") as capture:
                with self.assertRaisesRegex(ConfigError, "literal prefix"):
                    generate_lockfile(
                        _config("v*"),
                        Path(td),
                        source="head",
                    )
            capture.assert_not_called()

    def test_head_snapshot_resolves_unicode_namespaced_tag(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _initialize_component(root)
            prefix = "team/rélease-v"
            _git(root, "tag", f"{prefix}1.2.3")

            lock = generate_lockfile(_config(prefix), root, source="head")

            self.assertEqual(lock["components"]["svc"]["version"], "1.2.3")

    def test_valid_prefix_without_reachable_shallow_tag_is_not_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = Path(td)
            source = fixture / "source"
            source.mkdir()
            _initialize_component(source)
            prefix = "svc-v"
            _git(source, "tag", f"{prefix}1.2.3")
            (source / "svc" / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
            commit_all(source, "move beyond tagged commit")

            shallow = fixture / "shallow"
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--no-tags",
                    source.as_uri(),
                    str(shallow),
                ],
                check=True,
                capture_output=True,
            )
            self.assertEqual(
                _git(shallow, "rev-parse", "--is-shallow-repository").stdout.strip(),
                "true",
            )

            with self.assertRaisesRegex(
                ConfigError,
                "Configured version source did not produce a version",
            ) as raised:
                generate_lockfile(_config(prefix), shallow, source="head")

            self.assertNotIn("Invalid literal Git tag prefix", str(raised.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
