"""Aggregate diagnostic count/byte bounds for untrusted repository input."""

from __future__ import annotations

import builtins
import copy
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
from boundver._lockfile import (
    _generation_errors,
    _lockfile_schema_issues,
    _lockfile_structure_issues,
    generate_lockfile,
    verify_lockfile,
)
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


def _preflight_pair(*, components: int, version_error: str) -> tuple[dict, dict]:
    """Return a config and lockfile whose only preflight diagnostics are digest errors.

    ``_verify_lock_preflight_issues`` returns as soon as the schema or the
    structure pass reports anything, and on that path it forwards no digest
    errors at all. So the lock entry cloned here is a real one produced by
    ``generate_lockfile`` rather than a hand-written dictionary: it satisfies
    every structural rule, and the config is built from the same component
    names so that the project, component set and slice set all agree. Each
    clone carries exactly one version error and a settled boundary, which
    leaves ``_generation_errors`` as the sole producer of diagnostics.
    """
    seed_config = {
        "project": "diagnostic-bounds",
        "components": {"svc": _component(paths=[])},
        "slices": {},
    }
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "svc").mkdir()
        (root / "svc" / "value.txt").write_text("locked\n", encoding="utf-8")
        template = generate_lockfile(seed_config, root, source="working-tree")

    entry = template["components"]["svc"]
    names = [f"svc-{index:04d}" for index in range(components)]
    lockfile = {key: value for key, value in template.items() if key != "components"}
    lockfile["components"] = {}
    for name in names:
        clone = copy.deepcopy(entry)
        clone["path"] = name
        clone["version_errors"] = [version_error]
        clone["boundary_status"] = "ok"
        lockfile["components"][name] = clone
    config = {
        "project": seed_config["project"],
        "components": {
            name: _component(paths=[], path=name) for name in names
        },
        "slices": {},
    }
    return config, lockfile


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

    def test_external_text_equal_to_the_sentinel_remains_ordinary(self) -> None:
        diagnostics = BoundedDiagnosticList(
            ["first failure", DIAGNOSTIC_TRUNCATION_SENTINEL, "omitted"]
        )
        self.assertFalse(diagnostics.truncated)
        self.assertEqual(
            diagnostics,
            ["first failure", DIAGNOSTIC_TRUNCATION_SENTINEL, "omitted"],
        )

    def test_a_real_truncation_marker_propagates_between_collectors(self) -> None:
        source = BoundedDiagnosticList(
            [f"failure {index}" for index in range(MAX_DIAGNOSTIC_ITEMS + 1)]
        )
        diagnostics = BoundedDiagnosticList(source)

        self.assertTrue(diagnostics.truncated)
        self.assertEqual(diagnostics[-1], DIAGNOSTIC_TRUNCATION_SENTINEL)


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
            "schema": "boundary-lock/v4",
            "config_contract": "boundver-semantic-config/v3",
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

    def test_preflight_rebounds_an_already_truncated_generation_feed(self) -> None:
        """The verify preflight re-bounds a digest feed that had already truncated.

        OBL-OUTPUT-016 requires the DIAGNOSTICS TRUNCATED sentinel to appear in
        verify's issues exactly when diagnostics were dropped, and requires that
        condition to force exit code 2. ``_generation_errors`` already returns a
        bounded list, so the last entry it hands the preflight is the bare
        sentinel. The preflight then prefixes every message it forwards with
        ``LOCKED DIGEST ERROR``, and only the second ``BoundedDiagnosticList``
        wrap in ``_verify_lock_preflight_issues`` keeps that prefix off the
        sentinel and keeps the count and byte budgets in force.

        Nothing in the suite called ``_verify_lock_preflight_issues`` before this
        test. The nearest neighbour above reaches ``_lockfile_structure_issues``
        directly, so MUT-OUTPUT-472 could replace that wrap with a plain
        ``list(...)`` and leave the whole repository green, while the sentinel a
        reader sees became ``LOCKED DIGEST ERROR DIAGNOSTICS TRUNCATED: ...``
        and the reported list outgrew both budgets.
        """
        config, lockfile = _preflight_pair(
            components=1000,
            version_error="x" * 16_384,
        )

        # PREMISE: the fixture clears both structural passes, so the early
        # return inside the preflight is not taken and the digest feed is
        # actually reached. Its sibling below asserts the same thing on its own.
        self.assertEqual(_lockfile_schema_issues(lockfile), [])
        self.assertEqual(
            _lockfile_structure_issues(
                lockfile,
                running_version=core._get_version(),
            ),
            [],
        )

        issues = core._verify_lock_preflight_issues(config, lockfile)

        self.assertTrue(
            any(item.startswith("LOCKED DIGEST ERROR ") for item in issues),
            issues[:3],
        )
        self.assertEqual(issues[-1], DIAGNOSTIC_TRUNCATION_SENTINEL)
        self.assertNotIn(
            f"LOCKED DIGEST ERROR {DIAGNOSTIC_TRUNCATION_SENTINEL}",
            issues,
        )
        self.assertEqual(issues.count(DIAGNOSTIC_TRUNCATION_SENTINEL), 1)
        self.assertLessEqual(len(issues), MAX_DIAGNOSTIC_ITEMS)
        self.assertLessEqual(
            sum(len(item.encode("utf-8")) for item in issues),
            MAX_DIAGNOSTIC_BYTES,
        )
        self.assertEqual(core._drift_exit_code(issues), core.EXIT_USAGE)
        _assert_bounded(self, issues)

    def test_premise_the_preflight_fixture_clears_both_structural_passes(self) -> None:
        """The premise: the fixture above is a lock the preflight reads all the way.

        ``_verify_lock_preflight_issues`` returns the structural list untouched
        the moment the schema or structure pass reports anything, and a
        thousand malformed components would truncate that list on their own.
        The sentinel asserted by the test above would then come from the first
        wrap rather than the second, and MUT-OUTPUT-472 would be invisible by
        construction. This test states the premise separately: the lockfile is
        structurally clean, the config agrees with it on project, component set
        and slice set, and every component really does carry the version error
        that the digest feed is supposed to report.
        """
        config, lockfile = _preflight_pair(
            components=1000,
            version_error="x" * 16_384,
        )

        self.assertEqual(_lockfile_schema_issues(lockfile), [])
        self.assertEqual(
            _lockfile_structure_issues(
                lockfile,
                running_version=core._get_version(),
            ),
            [],
        )
        self.assertEqual(lockfile["project"], config["project"])
        self.assertEqual(set(lockfile["components"]), set(config["components"]))
        self.assertEqual(set(lockfile["slices"]), set(config["slices"]))
        self.assertEqual(len(lockfile["components"]), 1000)
        self.assertTrue(
            all(
                entry["version_errors"] == ["x" * 16_384]
                for entry in lockfile["components"].values()
            )
        )
        self.assertEqual(
            _generation_errors(lockfile)[-1],
            DIAGNOSTIC_TRUNCATION_SENTINEL,
        )

    def test_contrast_an_untruncated_digest_feed_is_forwarded_whole(self) -> None:
        """The contrast: a feed that fits keeps every error and grows no sentinel.

        OBL-OUTPUT-016 has two directions, and the assertion above only covers
        one of them. A preflight that appended the sentinel to every result, or
        that dropped diagnostics whenever it saw more than one, would satisfy
        that assertion and still be wrong. The same fixture reduced to three
        components with one short error each is well inside both budgets, so
        the preflight has to return those three prefixed messages and nothing
        else. The exit code stays 2 because a locked digest error is a safety
        issue in its own right, not because anything was omitted.
        """
        config, lockfile = _preflight_pair(
            components=3,
            version_error="short failure",
        )

        issues = core._verify_lock_preflight_issues(config, lockfile)

        self.assertEqual(
            issues,
            [
                "LOCKED DIGEST ERROR svc-0000: short failure",
                "LOCKED DIGEST ERROR svc-0001: short failure",
                "LOCKED DIGEST ERROR svc-0002: short failure",
            ],
        )
        self.assertNotIn(DIAGNOSTIC_TRUNCATION_SENTINEL, issues)
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
