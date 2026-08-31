"""Aggregate diagnostic count/byte bounds for untrusted repository input."""

from __future__ import annotations

import builtins
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import boundver._config as config_module
import boundver.core as core
from boundver._lockfile import _generation_errors, generate_lockfile, verify_lockfile
from boundver._utils import (
    BoundedDiagnosticList,
    DIAGNOSTIC_TRUNCATION_SENTINEL,
    MAX_DIAGNOSTIC_BYTES,
    MAX_DIAGNOSTIC_ITEM_BYTES,
    MAX_DIAGNOSTIC_ITEMS,
    ConfigError,
)


def _assert_bounded(testcase: unittest.TestCase, diagnostics: list[str]) -> None:
    testcase.assertLessEqual(len(diagnostics), MAX_DIAGNOSTIC_ITEMS)
    testcase.assertLessEqual(
        sum(len(item.encode("utf-8")) for item in diagnostics),
        MAX_DIAGNOSTIC_BYTES,
    )
    testcase.assertTrue(
        all(
            len(item.encode("utf-8")) <= MAX_DIAGNOSTIC_ITEM_BYTES
            or item == DIAGNOSTIC_TRUNCATION_SENTINEL
            for item in diagnostics
        )
    )


def _component(
    *,
    paths: list[str],
    consumers: list[str] | None = None,
    path: str = "svc",
) -> dict:
    component = {
        "path": path,
        "boundary": {"provider": "implicit", "paths": paths},
    }
    if consumers is not None:
        component["consumers"] = consumers
    return component


def _run_cli(root: Path, *arguments: str) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = 0
    with patch.object(sys, "argv", ["boundver", *arguments]), patch.object(
        core, "git_root", return_value=root
    ):
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                core.main()
        except SystemExit as exc:
            exit_code = int(exc.code) if exc.code is not None else 0
    return exit_code, stdout.getvalue(), stderr.getvalue()


class BoundedDiagnosticListTests(unittest.TestCase):
    def test_count_budget_ends_with_one_explicit_sentinel(self) -> None:
        produced = 0

        def values():
            nonlocal produced
            for index in range(1000):
                produced += 1
                yield f"failure {index}"

        diagnostics = BoundedDiagnosticList(values())

        self.assertEqual(len(diagnostics), MAX_DIAGNOSTIC_ITEMS)
        self.assertEqual(produced, MAX_DIAGNOSTIC_ITEMS)
        self.assertTrue(diagnostics.truncated)
        self.assertEqual(diagnostics[-1], DIAGNOSTIC_TRUNCATION_SENTINEL)
        self.assertEqual(diagnostics.count(DIAGNOSTIC_TRUNCATION_SENTINEL), 1)
        _assert_bounded(self, list(diagnostics))

    def test_utf8_byte_budget_ends_with_one_explicit_sentinel(self) -> None:
        values = [f"{index}:" + ("\u00e9" * 4096) for index in range(1000)]

        first = BoundedDiagnosticList(values)
        second = BoundedDiagnosticList(values)

        self.assertEqual(first, second)
        self.assertTrue(first.truncated)
        self.assertEqual(first[-1], DIAGNOSTIC_TRUNCATION_SENTINEL)
        self.assertEqual(first.count(DIAGNOSTIC_TRUNCATION_SENTINEL), 1)
        _assert_bounded(self, list(first))

    def test_imported_truncation_sentinel_keeps_collector_failed(self) -> None:
        diagnostics = BoundedDiagnosticList(
            ["first failure", DIAGNOSTIC_TRUNCATION_SENTINEL, "omitted"]
        )
        self.assertTrue(diagnostics.truncated)
        self.assertEqual(
            diagnostics,
            ["first failure", DIAGNOSTIC_TRUNCATION_SENTINEL],
        )


class ConfigDiagnosticBudgetTests(unittest.TestCase):
    def test_long_component_and_many_missing_paths_are_bounded(self) -> None:
        long_name = "component-" + ("x" * (16_384 - len("component-")))
        paths = [f"missing/{index:04d}.json" for index in range(1000)]
        config = {
            "project": "diagnostic-bounds",
            "components": {long_name: _component(paths=paths)},
            "slices": {},
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()

            errors = config_module.validate_config(config, root)

        self.assertEqual(errors[-1], DIAGNOSTIC_TRUNCATION_SENTINEL)
        self.assertNotIn(long_name, "\n".join(errors))
        _assert_bounded(self, errors)

    def test_long_slice_and_many_graph_references_are_bounded(self) -> None:
        long_slice = "slice-" + ("s" * (16_384 - len("slice-")))
        references = [f"missing-component-{index:04d}" for index in range(1000)]
        config = {
            "project": "diagnostic-bounds",
            "components": {"svc": _component(paths=[])},
            "slices": {
                long_slice: {
                    "mode": "exact",
                    "components": references,
                }
            },
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()

            errors = config_module.validate_config(config, root)

        self.assertEqual(errors[-1], DIAGNOSTIC_TRUNCATION_SENTINEL)
        self.assertNotIn(long_slice, "\n".join(errors))
        _assert_bounded(self, errors)

    def test_many_consumer_graph_references_are_bounded(self) -> None:
        consumers = [f"unknown-{index:04d}" for index in range(1000)]
        config = {
            "project": "diagnostic-bounds",
            "components": {
                "svc": _component(paths=[], consumers=consumers),
            },
            "slices": {},
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()

            errors = config_module.validate_config(config, root)

        self.assertEqual(errors[-1], DIAGNOSTIC_TRUNCATION_SENTINEL)
        _assert_bounded(self, errors)

    def test_schema_engine_stops_and_sorts_only_the_bounded_prefix(self) -> None:
        try:
            import jsonschema  # noqa: F401
        except ImportError:  # pragma: no cover - optional dependency
            self.skipTest("jsonschema is not installed")
        config = {
            "project": "diagnostic-bounds",
            "components": {f"svc-{index:04d}": None for index in range(1000)},
            "slices": {},
        }
        schema = config_module._load_config_schema(Path.cwd())

        first = config_module._schema_engine_errors(config, schema)
        second = config_module._schema_engine_errors(config, schema)

        self.assertEqual(first, second)
        self.assertEqual(first[-1], DIAGNOSTIC_TRUNCATION_SENTINEL)
        _assert_bounded(self, first)

    def test_dependency_free_hand_validation_has_the_same_bound(self) -> None:
        paths = [f"missing/{index:04d}.json" for index in range(1000)]
        config = {
            "project": "diagnostic-bounds",
            "components": {"svc": _component(paths=paths)},
            "slices": {},
        }
        real_import = builtins.__import__

        def import_without_jsonschema(name, *args, **kwargs):
            if name == "jsonschema" or name.startswith("jsonschema."):
                raise ImportError("jsonschema deliberately unavailable")
            return real_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            with patch("builtins.__import__", side_effect=import_without_jsonschema):
                errors = config_module.validate_config(config, root)

        self.assertEqual(errors[-1], DIAGNOSTIC_TRUNCATION_SENTINEL)
        _assert_bounded(self, errors)

    def test_human_and_verify_json_results_expose_truncation_and_failure(self) -> None:
        paths = [f"missing/{index:04d}.json" for index in range(1000)]
        config = {
            "project": "diagnostic-bounds",
            "components": {"svc": _component(paths=paths)},
            "slices": {},
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "boundary.config.json").write_text(
                json.dumps(config), encoding="utf-8"
            )

            human_code, human_stdout, _ = _run_cli(root, "validate-config")
            json_code, json_stdout, json_stderr = _run_cli(
                root,
                "verify",
                "--source",
                "working-tree",
                "--format",
                "json",
            )

        payload = json.loads(json_stdout)
        self.assertEqual(human_code, core.EXIT_USAGE)
        self.assertIn(DIAGNOSTIC_TRUNCATION_SENTINEL, human_stdout)
        self.assertEqual(json_code, core.EXIT_USAGE)
        self.assertEqual(json_stderr, "")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["issues"][-1], DIAGNOSTIC_TRUNCATION_SENTINEL)
        _assert_bounded(self, payload["issues"])


class GenerationAndVerificationDiagnosticAuditTests(unittest.TestCase):
    def test_generation_error_collection_is_bounded(self) -> None:
        lockfile = {
            "components": {
                f"svc-{index:04d}": {
                    "version_errors": ["x" * 16_384],
                    "boundary_status": "ok",
                }
                for index in range(1000)
            }
        }

        errors = _generation_errors(lockfile)

        self.assertEqual(errors[-1], DIAGNOSTIC_TRUNCATION_SENTINEL)
        _assert_bounded(self, errors)

    def test_generation_stops_producing_vendored_errors_at_the_budget(self) -> None:
        config = {
            "project": "diagnostic-bounds",
            "components": {
                "svc": {
                    **_component(paths=[]),
                    "vendored_copies": [
                        f"copies/{index:04d}" for index in range(1000)
                    ],
                }
            },
            "slices": {},
        }

        def content_digest(_root, path, **_kwargs):
            return "a" * 64 if path == "svc" else None

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "value.txt").write_text("value\n", encoding="utf-8")
            with patch(
                "boundver._lockfile._content_only_digest",
                side_effect=content_digest,
            ) as digest:
                with self.assertRaises(ConfigError) as raised:
                    generate_lockfile(config, root, source="working-tree")

        self.assertIn(DIAGNOSTIC_TRUNCATION_SENTINEL, str(raised.exception))
        self.assertLessEqual(digest.call_count, MAX_DIAGNOSTIC_ITEMS + 1)

    def test_lock_structure_and_machine_json_remain_bounded_and_failed(self) -> None:
        lockfile = {
            "schema": "boundary-lock/v3",
            "config_contract": "boundver-semantic-config/v2",
            "config_digest": "0" * 64,
            "project": "diagnostic-bounds",
            "components": {f"svc-{index:04d}": {} for index in range(1000)},
            "slices": {},
        }

        issues = core._lockfile_structure_issues(lockfile)
        payload = json.dumps({"ok": False, "issues": issues})

        self.assertEqual(issues[-1], DIAGNOSTIC_TRUNCATION_SENTINEL)
        self.assertIn(DIAGNOSTIC_TRUNCATION_SENTINEL, payload)
        self.assertEqual(core._drift_exit_code(issues), core.EXIT_USAGE)
        _assert_bounded(self, issues)

    def test_fail_fast_reports_truncation_as_its_single_safety_issue(self) -> None:
        config = {
            "project": "diagnostic-bounds",
            "components": {
                f"svc-{index}": _component(paths=[], path=f"svc-{index}")
                for index in range(5)
            },
            "slices": {},
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for index in range(5):
                component_root = root / f"svc-{index}"
                component_root.mkdir()
                (component_root / "value.txt").write_text(
                    "locked\n", encoding="utf-8"
                )
            lockfile = generate_lockfile(
                config,
                root,
                source="working-tree",
            )
            for index in range(5):
                (root / f"svc-{index}" / "value.txt").write_text(
                    "current\n", encoding="utf-8"
                )

            with patch("boundver._utils.MAX_DIAGNOSTIC_ITEMS", 4):
                issues = verify_lockfile(
                    config,
                    lockfile,
                    root,
                    source="working-tree",
                    fail_fast=True,
                )

        self.assertEqual(issues, [DIAGNOSTIC_TRUNCATION_SENTINEL])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
