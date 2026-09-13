"""The limit a review promises, and the bytes it actually writes.

`review --format text` refuses to emit a result over a declared ceiling, and
says so rather than truncating - a partial review would be worse than none. The
measurement it refuses on is taken over the raw lines, before printing, while
the printing itself escapes control characters and can fall back to
`backslashreplace` for a stream that cannot represent a code point. Both
transformations make text longer, so the number checked and the number written
are not the same number.

The ratios are large. A C0 byte becomes four characters, an astral character
written to a legacy console becomes ten, so a review measured just inside the
ceiling can be emitted several times over it. These tests hold the two
accountings against each other with the ceiling lowered, because the shipped
one is sixty-four megabytes and the mismatch does not need that much text to
be visible.

Covers OBL-OUTPUT-027.
"""

from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from boundver import _output, _review
from boundver._review import MAX_REVIEW_RESULT_BYTES, review_text_lines
from boundver._utils import GuardrailError

from tests._parity import run_cli
from tests._scenarios import Scenario

#: A ceiling small enough to reach in a test, standing in for the shipped one.
SMALL_LIMIT = 8192

_RESULT: dict = {}


def _review_result() -> dict:
    """One real review result, as the CLI produces it."""
    if not _RESULT:
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/main.py", "x\n")
            scene.commit()
            assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
            scene.commit("lock")
            base = scene.head()
            scene.append_line("svc/main.py", "y\n")
            scene.commit("edit")
            assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
            scene.commit("relock")
            result = run_cli(
                scene.root, "review", "--base", base, "--target", scene.head(),
                "--format", "json",
            )
            assert result.returncode == 0, result.stderr
            _RESULT.update(json.loads(result.stdout))
    return json.loads(json.dumps(_RESULT))


def _with_components(names) -> dict:
    """The same review, carrying components with the given names."""
    result = _review_result()
    result["components"]["changed"] = [
        {"name": name, "status": "changed", "facets": [], "metadata": []}
        for name in names
    ]
    return result


def _measured(lines) -> int:
    """The size the guard checks: raw lines plus one newline each."""
    return sum(
        len(line.encode("utf-8", errors="backslashreplace")) + 1 for line in lines
    )


def _emitted(lines, encoding: str = "utf-8") -> int:
    """The size a stream with that encoding actually receives."""
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding=encoding, newline="")
    for line in lines:
        _output.safe_print(line, file=stream)
    stream.flush()
    return len(buffer.getvalue())


class GuardAccountingTests(unittest.TestCase):
    """The premise: the guard fires, and on the measurement named above."""

    def test_plain_text_is_measured_the_way_it_is_written(self):
        lines = review_text_lines(_with_components(["svc", "sdk"]))
        self.assertEqual(_measured(lines), _emitted(lines))

    def test_the_shipped_ceiling_is_the_documented_one(self):
        self.assertEqual(MAX_REVIEW_RESULT_BYTES, 64 * 1024 * 1024)

    def test_a_review_over_the_ceiling_is_refused_rather_than_truncated(self):
        names = [f"component-{index:04d}" for index in range(SMALL_LIMIT // 4)]
        with mock.patch.object(_review, "MAX_REVIEW_RESULT_BYTES", SMALL_LIMIT):
            with self.assertRaises(GuardrailError) as raised:
                review_text_lines(_with_components(names))
        self.assertIn("No partial review result was emitted", str(raised.exception))

    def test_a_review_under_the_ceiling_is_rendered(self):
        with mock.patch.object(_review, "MAX_REVIEW_RESULT_BYTES", SMALL_LIMIT):
            lines = review_text_lines(_with_components(["svc"]))
        self.assertLessEqual(_measured(lines), SMALL_LIMIT)


class ControlCharacterExpansionTests(unittest.TestCase):
    """A name of control bytes: four characters written for each one."""

    def _lines(self):
        """Raw lines just inside the ceiling, dense in C0 bytes."""
        names = [chr(0x01) * 1000 for _index in range(6)]
        with mock.patch.object(_review, "MAX_REVIEW_RESULT_BYTES", SMALL_LIMIT):
            return review_text_lines(_with_components(names))

    def test_the_guard_refuses_the_expanded_review(self):
        with self.assertRaisesRegex(GuardrailError, "complete-output limit"):
            self._lines()

    def test_the_emitted_bytes_stay_inside_the_ceiling(self):
        with self.assertRaises(GuardrailError):
            self._lines()

    def test_a_small_control_value_is_escaped_and_accounted(self):
        with mock.patch.object(_review, "MAX_REVIEW_RESULT_BYTES", SMALL_LIMIT):
            lines = review_text_lines(_with_components([chr(0x01) * 100]))
        self.assertLessEqual(_emitted(lines), SMALL_LIMIT)

    def test_the_control_bytes_themselves_never_reach_the_stream(self):
        """A small accepted result still neutralizes its control bytes."""
        with mock.patch.object(_review, "MAX_REVIEW_RESULT_BYTES", SMALL_LIMIT):
            lines = review_text_lines(_with_components([chr(0x01) * 100]))
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="utf-8", newline="")
        for line in lines:
            _output.safe_print(line, file=stream)
        stream.flush()
        self.assertNotIn(b"\x01", buffer.getvalue())
        self.assertIn(b"\\x01", buffer.getvalue())


class LegacyEncodingExpansionTests(unittest.TestCase):
    """An astral name on a console that cannot represent it."""

    def _lines(self):
        names = [chr(0x1F600) * 250 for _index in range(6)]
        with mock.patch.object(_review, "MAX_REVIEW_RESULT_BYTES", SMALL_LIMIT):
            return review_text_lines(_with_components(names))

    def test_the_guard_refuses_a_value_that_could_expand_on_the_stream(self):
        with self.assertRaisesRegex(GuardrailError, "complete-output limit"):
            self._lines()

    def test_a_utf8_stream_receives_what_was_measured(self):
        """A smaller accepted value remains exact on UTF-8."""
        with mock.patch.object(_review, "MAX_REVIEW_RESULT_BYTES", SMALL_LIMIT):
            lines = review_text_lines(_with_components([chr(0x1F600) * 10]))
        self.assertEqual(_emitted(lines, "utf-8"), _measured(lines))

    def test_a_legacy_stream_stays_inside_the_ceiling(self):
        with self.assertRaises(GuardrailError):
            self._lines()

    def test_a_small_legacy_rendering_stays_inside_the_ceiling(self):
        with mock.patch.object(_review, "MAX_REVIEW_RESULT_BYTES", SMALL_LIMIT):
            lines = review_text_lines(_with_components([chr(0x1F600) * 10]))
        self.assertLessEqual(_emitted(lines, "cp1252"), SMALL_LIMIT)


if __name__ == "__main__":
    unittest.main()
