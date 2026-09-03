from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import tracemalloc
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from boundver._utils import DIAGNOSTIC_TRUNCATION_SENTINEL


REPO_ROOT = Path(__file__).parents[1]


def _load_script():
    path = REPO_ROOT / "scripts" / "export_action_outputs.py"
    spec = importlib.util.spec_from_file_location("boundver_action_outputs", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parse_github_output(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    parsed: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if "<<" not in line:
            name, value = line.split("=", 1)
            parsed[name] = value
            index += 1
            continue
        name, delimiter = line.split("<<", 1)
        index += 1
        values: list[str] = []
        while index < len(lines) and lines[index] != delimiter:
            values.append(lines[index])
            index += 1
        if index >= len(lines):
            raise AssertionError(f"unterminated output {name}")
        parsed[name] = "\n".join(values)
        index += 1
    return parsed


class ActionOutputTests(unittest.TestCase):
    def test_rejects_oversized_numeric_tokens(self):
        exporter = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.json"
            payloads = (
                b'{"n":'
                + (b"9" * (exporter.MAX_JSON_INTEGER_DIGITS + 1))
                + b"}",
                b'{"n":1.'
                + (b"0" * (exporter.MAX_JSON_NUMBER_CHARS + 1))
                + b"}",
                b'{"n":1e9999}',
            )
            for payload in payloads:
                with self.subTest(size=len(payload)):
                    path.write_bytes(payload)
                    value, status = exporter._load_payload(path, 64 * 1024)
                    self.assertEqual(value, {})
                    self.assertEqual(status, "invalid-json")

    def test_rejects_wide_or_deep_result_before_json_parser_allocation(self):
        exporter = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.json"
            path.write_bytes(b"[0,0,0]")
            with (
                mock.patch.object(exporter, "MAX_RESULT_JSON_TOKENS", 2),
                mock.patch.object(
                    exporter.json,
                    "loads",
                    side_effect=AssertionError("parser must not be called"),
                ) as loads,
            ):
                payload, status = exporter._load_payload(path, 1024)
            self.assertEqual(payload, {})
            self.assertEqual(status, exporter.SOURCE_OVER_COMPLEX)
            loads.assert_not_called()

            original = path.read_bytes()
            with mock.patch.object(exporter, "MAX_RESULT_JSON_TOKENS", 2):
                exporter.export_outputs(
                    path,
                    Path(temporary) / "github-output.txt",
                )
            self.assertEqual(path.read_bytes(), original)

            path.write_bytes(b"[[[0]]]")
            with (
                mock.patch.object(exporter, "MAX_RESULT_JSON_DEPTH", 2),
                mock.patch.object(
                    exporter.json,
                    "loads",
                    side_effect=AssertionError("parser must not be called"),
                ) as loads,
            ):
                payload, status = exporter._load_payload(path, 1024)
            self.assertEqual(payload, {})
            self.assertEqual(status, exporter.SOURCE_OVER_COMPLEX)
            loads.assert_not_called()

    def test_bounded_outputs_do_not_materialize_values_beyond_the_limit(self):
        exporter = _load_script()
        lines = ["\x1b" * 512] * 1_000
        tracemalloc.start()
        try:
            value, truncated = exporter._bounded_lines(
                lines,
                exporter.MAX_VALUE_UTF16_BYTES,
            )
            _current, line_peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertTrue(truncated)
        self.assertLessEqual(exporter._utf16_size(value), 64 * 1024)
        self.assertLess(line_peak, 2 * 1024 * 1024)

        rows = ["x" * 1_024] * 1_000
        tracemalloc.start()
        try:
            value, truncated = exporter._bounded_json_array(
                rows,
                exporter.MAX_VALUE_UTF16_BYTES,
            )
            _current, json_peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertTrue(truncated)
        self.assertEqual(value, "[]")
        self.assertLess(json_peak, 2 * 1024 * 1024)

    def test_exports_validation_truncation_sentinel_as_a_failed_issue(self):
        exporter = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = root / "result.json"
            output = root / "github-output"
            result.write_text(
                json.dumps(
                    {
                        "ok": False,
                        "issues": [
                            "first validation failure",
                            DIAGNOSTIC_TRUNCATION_SENTINEL,
                        ],
                        "observations": [],
                        "consumer_impact": [],
                    }
                ),
                encoding="utf-8",
            )

            truncated = exporter.export_outputs(result, output)
            values = _parse_github_output(output)

        self.assertEqual(truncated, ())
        self.assertIn(DIAGNOSTIC_TRUNCATION_SENTINEL, values["issues"])

    def test_exports_complete_values_and_result_path(self):
        exporter = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = root / "result.json"
            output = root / "github-output"
            payload = {
                "issues": ["first issue", "second issue"],
                "observations": ["one observation"],
                "consumer_impact": [
                    {
                        "component": "api",
                        "components": ["sdk"],
                        "external_consumers": [],
                        "facets": ["boundary"],
                        "transitive": False,
                    }
                ],
            }
            result.write_text(json.dumps(payload), encoding="utf-8")

            truncated = exporter.export_outputs(result, output)
            values = _parse_github_output(output)

            self.assertEqual(truncated, ())
            self.assertEqual(values["issues"], "first issue\nsecond issue")
            self.assertEqual(values["observations"], "one observation")
            self.assertEqual(
                json.loads(values["consumer-impact"]), payload["consumer_impact"]
            )
            self.assertEqual(json.loads(values["truncated-outputs"]), [])
            self.assertEqual(Path(values["result-file"]), result.resolve())

    @unittest.skipIf(
        sys.platform == "win32",
        "Windows filenames cannot contain control characters",
    )
    def test_result_path_is_exported_losslessly(self):
        exporter = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runner\ttemp\npath"
            root.mkdir()
            result = root / "result.json"
            output = Path(temporary) / "github-output"
            result.write_text(
                json.dumps(
                    {
                        "issues": [],
                        "observations": [],
                        "consumer_impact": [],
                    }
                ),
                encoding="utf-8",
            )

            exporter.export_outputs(result, output)
            values = _parse_github_output(output)

            self.assertEqual(values["result-file"], str(result.resolve()))

    def test_repository_text_cannot_forge_action_commands_or_terminal_output(self):
        exporter = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = root / "result.json"
            output = root / "github-output"
            payload = {
                "issues": [
                    "first\n::warning title=forged::not real",
                    "::error title=forged::not real",
                    "controls\r\t\x00\x1b[2J\x9b31m\u2028 café שלום",
                ],
                "observations": ["::notice::forged\nsecond line"],
                "consumer_impact": [
                    {
                        "component": "api\n::warning::machine-json",
                        "components": [],
                        "external_consumers": [],
                        "facets": ["boundary"],
                        "transitive": False,
                    }
                ],
            }
            result.write_text(json.dumps(payload), encoding="utf-8")

            truncated = exporter.export_outputs(result, output)
            values = _parse_github_output(output)

            self.assertEqual(truncated, ())
            self.assertEqual(
                values["issues"].splitlines(),
                [
                    "first\\n::warning title=forged::not real",
                    "\\x3a:error title=forged::not real",
                    "controls\\r\\t\\x00\\x1b[2J\\x9b31m\\u2028 café שלום",
                ],
            )
            self.assertEqual(
                values["observations"],
                "\\x3a:notice::forged\\nsecond line",
            )
            self.assertFalse(
                any(line.startswith("::") for line in values["issues"].splitlines())
            )
            self.assertEqual(
                json.loads(values["consumer-impact"]), payload["consumer_impact"]
            )
            self.assertEqual(json.loads(result.read_text(encoding="utf-8")), payload)

    def test_action_does_not_echo_an_invalid_source_value(self):
        action = (REPO_ROOT / "action.yml").read_text(encoding="utf-8")

        self.assertIn('echo "Invalid source input."', action)
        self.assertNotIn('echo "Invalid source: $BOUNDVER_SOURCE"', action)

    def test_oversized_values_are_bounded_and_explicit(self):
        exporter = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = root / "result.json"
            output = root / "github-output"
            payload = {
                "issues": ["i" * 500],
                "observations": ["o" * 500],
                "consumer_impact": [
                    {
                        "component": "api",
                        "components": [],
                        "external_consumers": ["x" * 500],
                        "facets": ["boundary"],
                        "transitive": True,
                    }
                ],
            }
            result.write_text(json.dumps(payload), encoding="utf-8")

            truncated = exporter.export_outputs(
                result, output, max_value_utf16_bytes=256
            )
            values = _parse_github_output(output)

            self.assertEqual(truncated, exporter.SIZED_OUTPUTS)
            self.assertEqual(values["issues"], exporter.TRUNCATION_MARKER)
            self.assertEqual(values["observations"], exporter.TRUNCATION_MARKER)
            self.assertEqual(values["consumer-impact"], "[]")
            self.assertEqual(
                tuple(json.loads(values["truncated-outputs"])), exporter.SIZED_OUTPUTS
            )
            for name in exporter.SIZED_OUTPUTS:
                self.assertLessEqual(exporter._utf16_size(values[name]), 256)
            self.assertEqual(json.loads(result.read_text()), payload)

    def test_oversized_result_fails_safe_without_losing_the_file(self):
        exporter = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = root / "result.json"
            output = root / "github-output"
            result.write_text(
                json.dumps(
                    {
                        "issues": ["issue"],
                        "observations": [],
                        "consumer_impact": [],
                    }
                ),
                encoding="utf-8",
            )

            truncated = exporter.export_outputs(
                result,
                output,
                max_value_utf16_bytes=256,
                max_result_bytes=8,
            )
            values = _parse_github_output(output)

            self.assertEqual(truncated, exporter.SIZED_OUTPUTS)
            self.assertEqual(values["consumer-impact"], "[]")
            self.assertEqual(Path(values["result-file"]), result.resolve())
            self.assertTrue(result.is_file())

    def test_missing_result_is_explicitly_incomplete_and_valid_json(self):
        exporter = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = root / "missing-result.json"
            output = root / "github-output"

            truncated = exporter.export_outputs(result, output)
            values = _parse_github_output(output)
            fallback = json.loads(result.read_text(encoding="utf-8"))

            self.assertEqual(truncated, exporter.SIZED_OUTPUTS)
            self.assertEqual(values["issues"], exporter.UNAVAILABLE_MARKER)
            self.assertEqual(values["observations"], exporter.UNAVAILABLE_MARKER)
            self.assertEqual(values["consumer-impact"], "[]")
            self.assertEqual(
                tuple(json.loads(values["truncated-outputs"])), exporter.SIZED_OUTPUTS
            )
            self.assertFalse(fallback["ok"])
            self.assertEqual(
                fallback["action_transport"],
                {"complete": False, "reason": "unreadable"},
            )

    def test_malformed_result_is_explicitly_incomplete_and_valid_json(self):
        exporter = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = root / "result.json"
            output = root / "github-output"
            result.write_text("not-json", encoding="utf-8")

            truncated = exporter.export_outputs(result, output)
            values = _parse_github_output(output)
            fallback = json.loads(result.read_text(encoding="utf-8"))

            self.assertEqual(truncated, exporter.SIZED_OUTPUTS)
            self.assertEqual(values["issues"], exporter.UNAVAILABLE_MARKER)
            self.assertEqual(values["consumer-impact"], "[]")
            self.assertEqual(
                tuple(json.loads(values["truncated-outputs"])), exporter.SIZED_OUTPUTS
            )
            self.assertEqual(
                fallback["action_transport"],
                {"complete": False, "reason": "invalid-json"},
            )

    def test_empty_result_is_explicitly_incomplete_and_valid_json(self):
        exporter = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = root / "result.json"
            output = root / "github-output"
            result.touch()

            truncated = exporter.export_outputs(result, output)
            values = _parse_github_output(output)
            fallback = json.loads(result.read_text(encoding="utf-8"))

            self.assertEqual(truncated, exporter.SIZED_OUTPUTS)
            self.assertEqual(values["consumer-impact"], "[]")
            self.assertEqual(
                tuple(json.loads(values["truncated-outputs"])), exporter.SIZED_OUTPUTS
            )
            self.assertEqual(
                fallback["action_transport"],
                {"complete": False, "reason": "empty"},
            )

    def test_review_plan_exports_machine_selections_summary_and_exact_annotation(self):
        exporter = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = root / "plan.json"
            summary = root / "summary.md"
            output = root / "github-output"
            commit = "a" * 40
            payload = {
                "schema": "boundver-plan/v1",
                "complete": True,
                "endpoints": {"target": {"commit": commit}},
                "consumer_impact": [{"component": "api"}],
                "selection": {
                    "changed_components": ["api"],
                    "impacted_components": ["client"],
                    "external_consumers": ["mobile"],
                    "test_components": ["api", "client"],
                    "changed_slices": ["contracts"],
                    "impacted_slices": ["frontends"],
                    "test_slices": ["contracts", "frontends"],
                },
                "source_locations": [
                    {
                        "component": "api\n::error::forged\x1b[31m",
                        "path": "svc/api:%\n,contract\x1b[31m.json",
                        "endpoint": "target",
                        "commit": commit,
                    }
                ],
            }
            result.write_text(json.dumps(payload), encoding="utf-8")
            summary.write_text("summary\n", encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                truncated = exporter.export_outputs(
                    result,
                    output,
                    operation="review",
                    summary_path=summary,
                    annotation_commit=commit,
                )
            values = _parse_github_output(output)

            self.assertEqual(truncated, ())
            self.assertEqual(values["result-schema"], "boundver-plan/v1")
            self.assertEqual(values["transport-complete"], "true")
            self.assertEqual(values["selection-complete"], "true")
            self.assertEqual(json.loads(values["changed-components"]), ["api"])
            self.assertEqual(json.loads(values["impacted-components"]), ["client"])
            self.assertEqual(json.loads(values["test-components"]), ["api", "client"])
            self.assertEqual(Path(values["summary-file"]), summary.resolve())
            annotation = stderr.getvalue()
            self.assertIn(
                "::notice file=svc/api%3A%25%0A%2Ccontract\\x1b[31m.json",
                annotation,
            )
            self.assertNotIn("\n::error::forged", annotation)
            self.assertIn("api%0A::error::forged", annotation)
            self.assertNotIn("\x1b", annotation)
            self.assertIn("\\x1b[31m", annotation)

    def test_review_plan_bounded_selection_fails_closed_to_empty_array(self):
        exporter = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = root / "plan.json"
            output = root / "github-output"
            payload = {
                "schema": "boundver-plan/v1",
                "complete": True,
                "selection": {
                    "changed_components": [],
                    "impacted_components": ["x" * 500],
                },
                "consumer_impact": [],
            }
            result.write_text(json.dumps(payload), encoding="utf-8")

            truncated = exporter.export_outputs(
                result,
                output,
                operation="review",
                max_value_utf16_bytes=256,
            )
            values = _parse_github_output(output)

            self.assertEqual(values["impacted-components"], "[]")
            self.assertEqual(values["selection-complete"], "false")
            self.assertIn("impacted-components", truncated)
            self.assertIn(
                "impacted-components",
                json.loads(values["truncated-outputs"]),
            )
            self.assertEqual(Path(values["result-file"]), result.resolve())

    def test_review_operation_rejects_non_plan_result_as_incomplete(self):
        exporter = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = root / "result.json"
            output = root / "github-output"
            result.write_text(json.dumps({"ok": True}), encoding="utf-8")

            truncated = exporter.export_outputs(
                result,
                output,
                operation="review",
            )
            values = _parse_github_output(output)
            fallback = json.loads(result.read_text(encoding="utf-8"))

            self.assertEqual(values["transport-complete"], "false")
            self.assertEqual(values["selection-complete"], "false")
            self.assertEqual(values["result-schema"], "")
            self.assertEqual(
                set(truncated),
                set(exporter.SIZED_OUTPUTS) | set(exporter.PLAN_ARRAY_OUTPUTS),
            )
            self.assertEqual(
                fallback["action_transport"],
                {"complete": False, "reason": "invalid-plan"},
            )

    def test_review_exporter_cli_returns_usage_error_for_non_plan_result(self):
        exporter = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = root / "result.json"
            output = root / "github-output"
            result.write_text(json.dumps({"ok": True}), encoding="utf-8")

            code = exporter.main(
                [
                    "--result",
                    str(result),
                    "--github-output",
                    str(output),
                    "--operation",
                    "review",
                ]
            )

            self.assertEqual(code, 2)
            self.assertEqual(
                _parse_github_output(output)["transport-complete"],
                "false",
            )


if __name__ == "__main__":
    unittest.main()
