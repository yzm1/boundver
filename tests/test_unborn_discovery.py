"""Unborn and non-Git component-discovery source contracts."""

from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from boundver import _discovery as discovery
from boundver._config import discover_components
from boundver._utils import ConfigError, GuardrailError
from tests._repo_fixtures import init_git_repo


def _write_package(root: Path, relative: str) -> None:
    package = root / relative
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_text(
        '{"name":"fixture","version":"1.0.0"}\n',
        encoding="utf-8",
    )


class UnbornGitDiscoveryTests(unittest.TestCase):
    def test_git_controls_complete_unborn_discovery_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / ".gitignore").write_text(
                "generated/\n"
                "packages/*/package.json\n"
                "!packages/real/package.json\n"
                "packages/real/openapi.yaml\n",
                encoding="utf-8",
            )
            (root / "packages").mkdir()
            (root / "packages" / ".gitignore").write_text(
                "nested/*/package.json\n"
                "!nested/kept/package.json\n",
                encoding="utf-8",
            )
            global_excludes = root / ".global-excludes"
            global_excludes.write_text("globally-hidden/\n", encoding="utf-8")
            subprocess.run(
                ["git", "config", "core.excludesFile", str(global_excludes)],
                cwd=root,
                check=True,
                capture_output=True,
            )

            for relative in (
                "generated/fake/package.json",
                "packages/fake/package.json",
                "packages/real/package.json",
                "packages/nested/dropped/package.json",
                "packages/nested/kept/package.json",
                "globally-hidden/package.json",
            ):
                _write_package(root, relative)
            (root / "packages" / "real" / "openapi.yaml").write_text(
                "openapi: 3.0.0\npaths: {}\n", encoding="utf-8"
            )
            (root / "packages" / "real" / "api.json").write_text(
                "{}\n", encoding="utf-8"
            )

            embedded = root / "embedded"
            embedded.mkdir()
            init_git_repo(embedded)
            _write_package(root, "embedded/package.json")

            found = discover_components(root)

            self.assertEqual(set(found), {"kept", "real"})
            self.assertEqual(
                found["real"]["boundary"],
                {"provider": "json-file", "paths": ["api.json"]},
            )
            self.assertEqual(
                found["kept"]["boundary"],
                {"provider": "implicit", "paths": []},
            )

    def test_unborn_manifest_budget_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            _write_package(root, "one/package.json")
            _write_package(root, "two/package.json")

            with patch("boundver._config.MAX_DISCOVERY_MANIFESTS", 1):
                with self.assertRaisesRegex(GuardrailError, ">1 manifests"):
                    discover_components(root)

    def test_bootstrap_git_failure_does_not_crawl_the_repository(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            _write_package(root, "real/package.json")
            failure = subprocess.CalledProcessError(
                73,
                ["git", "ls-files", "--others"],
            )

            with patch.object(
                discovery,
                "_list_unborn_working_tree_paths",
                side_effect=failure,
            ):
                with self.assertRaisesRegex(
                    ConfigError,
                    "could not enumerate non-ignored bootstrap files",
                ):
                    discover_components(root)

    def test_index_failure_in_real_repository_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            failure = subprocess.CalledProcessError(73, ["git", "ls-files"])

            with patch.object(
                discovery,
                "_iter_bounded_git_paths",
                side_effect=failure,
            ):
                with self.assertRaisesRegex(
                    ConfigError,
                    "refusing a filesystem approximation",
                ):
                    discover_components(root)


class NonGitDiscoveryTests(unittest.TestCase):
    def test_fallback_is_bounded_and_visibly_approximate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".gitignore").write_text("generated/\n", encoding="utf-8")
            _write_package(root, "generated/fake/package.json")
            _write_package(root, "real/package.json")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                found = discover_components(root)

            self.assertEqual(set(found), {"fake", "real"})
            warning = stderr.getvalue()
            self.assertIn("filesystem approximation", warning)
            self.assertIn(".gitignore", warning)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
