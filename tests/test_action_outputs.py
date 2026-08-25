from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
