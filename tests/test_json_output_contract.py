"""What reaches stdout when a machine is reading it.

Three questions with one audience. Whether a pre-parse scanner can be fooled
by putting structure inside a string, whether a failed write can leave half a
document behind, and whether every exit path of a --format json command emits
one.

Covers OBL-OUTPUT-001, OBL-OUTPUT-003, OBL-OUTPUT-008 and OBL-OUTPUT-009.
"""

from __future__ import annotations

import io
import json
import unittest

from boundver._output import _display_value, _print_json
from boundver._structured_data import (
    StrictJSONError,
    _reject_excessive_json_tokens,
)

from tests._parity import run_cli
from tests._scenarios import Scenario


def _refuses(text: str) -> bool:
    try:
        _reject_excessive_json_tokens(text)
    except StrictJSONError:
        return True
    return False


def _widest_accepted() -> str:
    """A structure just wide enough to be refused, found by doubling."""
    count = 1
    while count <= 2 ** 22:
        if _refuses('{"a": 1}' * count):
            return '{"a": 1}' * count
        count *= 2
    raise AssertionError("the scanner never refused a widening structure")


class TokenScannerTests(unittest.TestCase):
    """OBL-OUTPUT-001: structure inside a string is text, not structure."""

    def test_an_over_wide_structure_is_refused(self):
        self.assertTrue(_refuses(_widest_accepted()))

    def test_the_same_characters_inside_a_string_are_accepted(self):
        """Wrapping a refused document makes one string token, not thousands."""
        refused = _widest_accepted()
        wrapped = json.dumps({"k": refused})
        self.assertGreater(len(wrapped), len(refused))
        self.assertFalse(_refuses(wrapped))

    def test_escapes_inside_a_string_do_not_end_it_early(self):
        """An escaped quote must not let the scanner start counting again."""
        payload = '\\"' + '{"a": 1}' * 200
        self.assertFalse(_refuses(json.dumps({"k": payload})))

    def test_an_escaped_backslash_does_not_swallow_the_closing_quote(self):
        """`"a\\\\"` ends the string; the structure after it must count."""
        refused = _widest_accepted()
        document = '{"k": "a' + chr(92) + chr(92) + '", "v": ' + refused + "}"
        self.assertTrue(_refuses(document))


class PrintJsonAtomicityTests(unittest.TestCase):
    """OBL-OUTPUT-003: the whole document, or none of it."""

    def test_a_text_stream_writes_nothing_before_an_encode_error(self):
        """The premise behind the obligation's concern, measured.

        It expects the recovery path to duplicate a prefix that a failed
        write already emitted. There is no such prefix: TextIOWrapper encodes
        the whole string before any bytes reach the buffer.
        """
        for prefix in (10, 4096, 100_000):
            with self.subTest(prefix=prefix):
                buffer = io.BytesIO()
                stream = io.TextIOWrapper(buffer, encoding="ascii", newline="\n")
                with self.assertRaises(UnicodeEncodeError):
                    stream.write("a" * prefix + "中" + "b" * 10)
                self.assertEqual(buffer.getvalue(), b"")

    def test_a_document_a_stream_cannot_encode_is_written_once_escaped(self):
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="ascii", newline="\n")
        original = __import__("sys").stdout
        __import__("sys").stdout = stream
        try:
            _print_json({"name": "中文"})
            stream.flush()
        finally:
            __import__("sys").stdout = original
        written = buffer.getvalue().decode("ascii")
        self.assertEqual(written.count('"name"'), 1)
        self.assertIn("name", json.loads(written))

    def test_an_encodable_document_is_written_verbatim(self):
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="utf-8", newline="\n")
        original = __import__("sys").stdout
        __import__("sys").stdout = stream
        try:
            _print_json({"b": 2, "a": 1})
            stream.flush()
        finally:
            __import__("sys").stdout = original
        written = buffer.getvalue().decode("utf-8")
        self.assertEqual(json.loads(written), {"a": 1, "b": 2})
        self.assertEqual(written.count('"a"'), 1)


class DisplayValueBoundTests(unittest.TestCase):
    """OBL-OUTPUT-009: metadata in human output must be bounded like the rest."""

    def test_a_small_value_renders_normally(self):
        self.assertEqual(json.loads(_display_value({"a": 1})), {"a": 1})

    def test_a_large_value_is_bounded(self):
        """Known divergence: _display_value passes no max_bytes.

        _bounded_json_dumps defaults max_bytes to None, so the only bounded
        thing about this call is its name. A five megabyte metadata string
        renders in full into human output.
        """
        rendered = _display_value({"k": "x" * 5_000_000})
        self.assertLess(len(rendered.encode("utf-8")), 1_000_000)

    def test_the_display_value_uses_the_diagnostic_bound(self):
        payload = "x" * 5_000_000
        rendered = _display_value({"k": payload})
        self.assertLess(len(rendered.encode("utf-8")), len(payload))
        self.assertNotIn(payload, rendered)


class WhyJsonExitPathTests(unittest.TestCase):
    """OBL-OUTPUT-008: a --format json command and its error paths."""

    def _repository(self) -> Scenario:
        scene = Scenario()
        scene.component("svc", path="svc", boundary=["api"])
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.commit()
        run_cli(scene.root, "generate", "--source", "head")
        scene.commit("lock")
        return scene

    def _stdout(self, scene: Scenario, *arguments):
        result = run_cli(scene.root, "why", *arguments, "--format", "json")
        return result.returncode, (result.stdout or "").strip()

    def test_the_ordinary_paths_emit_one_document(self):
        with self._repository() as scene:
            for label, component in (("clean", "svc"),):
                with self.subTest(case=label):
                    code, body = self._stdout(scene, component, "--source", "head")
                    self.assertTrue(body)
                    self.assertIsInstance(json.loads(body), dict)
            scene.append_line("svc/api/v1.yaml", "drift\n")
            scene.commit("drift")
            code, body = self._stdout(scene, "svc", "--source", "head")
            self.assertIsInstance(json.loads(body), dict)

    def test_stdout_is_never_polluted_with_prose(self):
        """The half that holds: nothing but JSON or nothing at all."""
        with self._repository() as scene:
            for component in ("svc", "nope"):
                with self.subTest(component=component):
                    _code, body = self._stdout(scene, component, "--source", "head")
                    if body:
                        json.loads(body)

    def test_every_exit_path_emits_a_document(self):
        """An unknown component still emits a machine-readable refusal."""
        with self._repository() as scene:
            _code, body = self._stdout(scene, "nope", "--source", "head")
            self.assertEqual(
                json.loads(body),
                {
                    "component": "nope",
                    "error": "unknown component 'nope'",
                    "known_components": ["svc"],
                },
            )

    def test_a_usage_error_is_json_and_keeps_the_usage_exit_code(self):
        with self._repository() as scene:
            code, body = self._stdout(scene, "nope", "--source", "head")
            self.assertEqual(code, 2)
            self.assertIsInstance(json.loads(body), dict)


if __name__ == "__main__":
    unittest.main()
