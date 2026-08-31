"""Operation-wide path-glob compilation and matching guardrails."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import boundver._utils as utils
from boundver._config import _expand_component_paths, validate_config
from boundver._output import analyze_explain_changes
from boundver._utils import GuardrailError, _PathGlobOperation, _match_path_glob
from boundver.providers import PathHashProvider, ProviderContext
from tests._repo_fixtures import commit_all, init_git_repo


def _provider_context(pattern: str, *, matching: bool) -> ProviderContext:
    files = {
        f"svc/file-{index}.txt": b"contract\n"
        for index in range(20)
    }
    selected_pattern = pattern if matching else "missing-*.txt"

    def read_file(path: str) -> bytes:
        return files[path]

    def list_files(prefix: str) -> list[str]:
        prefix = prefix.rstrip("/")
        return sorted(
            path
            for path in files
            if path == prefix or path.startswith(prefix + "/")
        )

    return ProviderContext(
        repo_root=Path("/repo"),
        component_path="svc",
        boundary_cfg={"paths": [selected_pattern]},
        source="working-tree",
        read_file=read_file,
        list_files=list_files,
    )


class CompiledPathGlobTests(unittest.TestCase):
    def test_operation_compiles_each_normalized_pattern_once(self) -> None:
        operation = _PathGlobOperation("test", max_steps=100_000)
        expected = []
        actual = []
        for index in range(100):
            candidate = f"api/route-{index}.yaml"
            expected.append(_match_path_glob(candidate, "api/*.yaml"))
            actual.append(operation.matches(candidate, "api/*.yaml"))

        self.assertEqual(actual, expected)
        self.assertEqual(operation.compiled_patterns, 1)

    def test_representative_valid_cross_product_fits_default_budget(self) -> None:
        patterns = [f"group-{index:02d}/*.json" for index in range(32)]
        candidates = [
            f"group-{index % 32:02d}/contract-{index}.json"
            for index in range(5_000)
        ]
        operation = _PathGlobOperation("representative corpus")
        matched = 0

        for pattern in patterns:
            operation.prepare(pattern)
            for candidate in candidates:
                matched += operation.matches(candidate, pattern)

        self.assertEqual(matched, len(candidates))
        self.assertEqual(operation.compiled_patterns, len(patterns))
        self.assertLess(operation.steps, utils.MAX_GLOB_OPERATION_STEPS // 4)

    def test_matching_and_nonmatching_provider_cross_products_fail_closed(self) -> None:
        for matching in (True, False):
            with self.subTest(matching=matching), patch.object(
                utils,
                "MAX_GLOB_OPERATION_STEPS",
                50,
            ):
                resolved = PathHashProvider().resolve(
                    _provider_context("file-*.txt", matching=matching)
                )

            self.assertEqual(resolved.status, "error")
            self.assertEqual(len(resolved.errors), 1)
            self.assertIn("aggregate glob compile/match steps", resolved.errors[0])
            self.assertIn("reduce wildcard declarations", resolved.errors[0])

    def test_literal_provider_path_does_not_consume_glob_budget(self) -> None:
        files = {"svc/file.txt": b"contract\n"}
        context = ProviderContext(
            repo_root=Path("/repo"),
            component_path="svc",
            boundary_cfg={"paths": ["file.txt"]},
            source="working-tree",
            read_file=files.__getitem__,
            list_files=lambda prefix: [
                path
                for path in files
                if path == prefix or path.startswith(prefix.rstrip("/") + "/")
            ],
        )

        with patch.object(utils, "MAX_GLOB_OPERATION_STEPS", 0):
            resolved = PathHashProvider().resolve(context)

        self.assertEqual(resolved.status, "ok", resolved.errors)
        self.assertEqual(resolved.entries, [("file:file.txt", b"contract\n")])


class PrimaryGlobOperationTests(unittest.TestCase):
    def _repo_with_files(self, root: Path, count: int = 20) -> None:
        init_git_repo(root)
        component = root / "svc"
        component.mkdir()
        for index in range(count):
            (component / f"file-{index}.txt").write_text(
                "contract\n",
                encoding="utf-8",
            )
        commit_all(root)

    def test_config_expansion_budget_covers_every_source_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo_with_files(root)

            for source in ("head", "index", "working-tree"):
                with self.subTest(source=source), patch.object(
                    utils,
                    "MAX_GLOB_OPERATION_STEPS",
                    50,
                ):
                    with self.assertRaisesRegex(
                        GuardrailError,
                        "aggregate glob compile/match steps",
                    ):
                        _expand_component_paths(
                            root,
                            "svc",
                            ["file-*.txt"],
                            source=source,
                        )

    def test_validation_shares_one_budget_across_boundary_and_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo_with_files(root)
            config = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {
                            "provider": "path-hash",
                            "paths": ["file-*.txt"],
                        },
                        "behavior": {"paths": ["file-*.txt"]},
                    }
                },
            }

            with patch.object(utils, "MAX_GLOB_OPERATION_STEPS", 1_500):
                errors = validate_config(config, root, source="head")

            aggregate_errors = [
                error
                for error in errors
                if "aggregate glob compile/match steps" in error
            ]
            self.assertEqual(len(aggregate_errors), 1, errors)
            self.assertIn(
                "path expansion could not be validated",
                aggregate_errors[0],
            )

    def test_explain_returns_one_actionable_budget_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo_with_files(root)
            for path in sorted((root / "svc").glob("*.txt")):
                path.write_text("changed\n", encoding="utf-8")
            config = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {
                            "provider": "path-hash",
                            "paths": ["file-*.txt"],
                        },
                    }
                },
            }

            with patch.object(utils, "MAX_GLOB_OPERATION_STEPS", 50):
                result = analyze_explain_changes(
                    config,
                    root,
                    "svc",
                    base_ref="HEAD",
                    source="working-tree",
                )

            self.assertEqual(
                set(result),
                {"error"},
            )
            self.assertIn("failed closed", result["error"])
            self.assertIn("reduce wildcard declarations", result["error"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
