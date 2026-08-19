"""Encoding portability contracts for human-readable CLI output."""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import boundver._output as output
import boundver.core as core
from boundver._output import (
    configure_cli_streams,
    explain_component_changes,
    print_diff,
    print_status,
    safe_print,
    why_component,
)
from boundver._utils import _bounded_json_int


class LegacyCodePageOutputTests(unittest.TestCase):
    def _capture_cp1252(self, callback) -> str:
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        try:
            with patch.object(sys, "stdout", stream):
                callback()
                stream.flush()
            return raw.getvalue().decode("cp1252")
        finally:
            stream.detach()

    def test_safe_print_preserves_unencodable_codepoints_as_escapes(self):
        output = self._capture_cp1252(lambda: safe_print("name=svc\U0001f600"))
        self.assertEqual(output, "name=svc\\U0001f600\r\n" if sys.platform == "win32" else "name=svc\\U0001f600\n")

    def test_cli_entrypoint_configures_stdout_and_stderr_before_argparse(self):
        calls: list[tuple[str, dict[str, str]]] = []

        class ReconfigurableStream(io.StringIO):
            def __init__(self, label: str):
                super().__init__()
                self.label = label

            def reconfigure(self, **kwargs):
                calls.append((self.label, kwargs))

        stdout = ReconfigurableStream("stdout")
        stderr = ReconfigurableStream("stderr")
        with patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr), \
             patch.object(sys, "argv", ["boundver", "--help"]):
            with self.assertRaises(SystemExit) as raised:
                core.main()

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(
            calls,
            [
                ("stdout", {"errors": "backslashreplace"}),
                ("stderr", {"errors": "backslashreplace"}),
            ],
        )

    def test_cli_stream_reconfiguration_covers_argparse_and_direct_writes(self):
        stdout_raw = io.BytesIO()
        stderr_raw = io.BytesIO()
        stdout = io.TextIOWrapper(stdout_raw, encoding="cp1252", errors="strict")
        stderr = io.TextIOWrapper(stderr_raw, encoding="cp1252", errors="strict")
        try:
            with patch.object(sys, "stdout", stdout), patch.object(
                sys, "stderr", stderr
            ):
                configure_cli_streams()
                self.assertEqual(stdout.errors, "backslashreplace")
                self.assertEqual(stderr.errors, "backslashreplace")
                stdout.write("project\U0001f600")
                stderr.write("error\U0001f600")
                stdout.flush()
                stderr.flush()
            self.assertIn(b"project\\U0001f600", stdout_raw.getvalue())
            self.assertIn(b"error\\U0001f600", stderr_raw.getvalue())
        finally:
            stdout.detach()
            stderr.detach()

    def test_status_handles_unicode_project_metadata(self):
        output = self._capture_cp1252(
            lambda: print_status(
                {
                    "project": "project\U0001f600",
                    "components": {},
                    "slices": {},
                }
            )
        )
        self.assertIn("project\\U0001f600", output)

    def test_diff_handles_unicode_metadata(self):
        output = self._capture_cp1252(
            lambda: print_diff(
                {
                    "changed_metadata": {
                        "project": {"old": "old", "new": "new\U0001f600"}
                    },
                    "components": {
                        "added": [],
                        "removed": [],
                        "changed": [],
                        "unchanged": [],
                    },
                    "slices": {
                        "added": [],
                        "removed": [],
                        "changed": [],
                        "unchanged": [],
                    },
                }
            )
        )
        self.assertIn("new\\ud83d\\ude00", output)

    @unittest.skipUnless(
        hasattr(sys, "set_int_max_str_digits"),
        "runtime has no configurable integer conversion limit",
    )
    def test_diff_ignores_lower_runtime_integer_limit(self):
        digits = "9" * 1000
        number = _bounded_json_int(digits)
        previous = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(640)
            rendered = self._capture_cp1252(
                lambda: print_diff(
                    {
                        "changed_metadata": {
                            "sequence": {"old": 0, "new": number}
                        },
                        "components": {
                            "added": [],
                            "removed": [],
                            "changed": [],
                            "unchanged": [],
                        },
                        "slices": {
                            "added": [],
                            "removed": [],
                            "changed": [],
                            "unchanged": [],
                        },
                    }
                )
            )
        finally:
            sys.set_int_max_str_digits(previous)
        self.assertIn(digits, rendered)

    def test_explain_handles_unicode_component_name_and_path(self):
        analysis = {
            "component_path": "svc/\U0001f600",
            "effective_base": "HEAD",
            "base_ref": "HEAD",
            "source": "head",
            "changed": [],
            "boundary_provider": "implicit",
            "boundary_paths": [],
            "boundary_changed": [],
        }
        with patch.object(output, "analyze_explain_changes", return_value=analysis):
            rendered = self._capture_cp1252(
                lambda: explain_component_changes(
                    {}, Path("."), "svc\U0001f600", source="head"
                )
            )
        self.assertIn("Component: svc\\U0001f600", rendered)
        self.assertIn("Path: svc/\\U0001f600", rendered)

    def test_why_handles_unicode_component_name(self):
        analysis = {
            "changes": [],
            "metadata_changes": {},
            "digest_errors": [],
            "version": None,
        }
        config = {"components": {"svc\U0001f600": {"path": "svc/\U0001f600"}}}
        with patch.object(output, "analyze_component_drift", return_value=analysis):
            rendered = self._capture_cp1252(
                lambda: why_component(
                    config,
                    {},
                    Path("."),
                    "svc\U0001f600",
                    source="working-tree",
                )
            )
        self.assertIn("Component:  svc\\U0001f600", rendered)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
