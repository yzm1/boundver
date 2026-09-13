"""A helper that must never fail, given a provider that is trying to make it.

`explain_provider_diff` calls into third-party code and then has to produce a
string whatever comes back. Its fallback table is complete: no attribute, an
attribute that raises on access, a non-callable, a non-string return, a blank
return and an over-long return all yield the same sentence. What it does not
guard is the shape of the string itself. A str subclass is a string by
isinstance and can still redefine the two methods the helper calls on it.

It also does not bound what the accepted string becomes on its way to a
sink, which is a separate question with a separate answer.

Covers OBL-PROVIDERS-048, OBL-PROVIDERS-051 and OBL-PROVIDERS-052.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

import boundver._output as output
from boundver._utils import _bounded_json_dumps
from boundver.providers import MAX_PROVIDER_ERROR_BYTES, explain_provider_diff

FALLBACK = "demo boundary changed"


class _Provider:
    """A provider with a name and nothing else."""

    name = "demo"


def _returning(value) -> _Provider:
    """A provider whose explain_diff hands back *value*."""

    class Returns(_Provider):
        def explain_diff(self, old, new, ctx):
            return value

    return Returns()


def _explained(provider) -> str:
    return explain_provider_diff(provider, None, None, None)


class AttributeRaises(_Provider):
    @property
    def explain_diff(self):
        raise RuntimeError("attribute access exploded")


class NotCallable(_Provider):
    explain_diff = "not a function"


class CallRaises(_Provider):
    def explain_diff(self, old, new, ctx):
        raise ValueError("boom")


class LyingEncode(str):
    """Under-reports its own UTF-8 length."""

    def encode(self, *args, **kwargs):
        return b""


class LyingBoth(LyingEncode):
    """Under-reports its length and refuses to be laundered by strip."""

    def strip(self, *args, **kwargs):
        return self


class StripRaises(str):
    def strip(self, *args, **kwargs):
        raise RuntimeError("strip exploded")


class StripReturnsNonString(str):
    def strip(self, *args, **kwargs):
        return 7


class FallbackTableTests(unittest.TestCase):
    """OBL-PROVIDERS-048: five ways to have nothing to say, one sentence."""

    def test_a_provider_that_explains_itself_is_believed(self):
        """The premise: the fallback is not the only thing this can return."""
        self.assertEqual(_explained(_returning("two paths differ")), "two paths differ")

    def test_every_degenerate_provider_gets_the_same_sentence(self):
        cases = {
            "no attribute": _Provider(),
            "attribute raises": AttributeRaises(),
            "not callable": NotCallable(),
            "returns a non-string": _returning(42),
            "returns None": _returning(None),
            "returns whitespace": _returning("   "),
            "returns too many bytes": _returning("x" * (MAX_PROVIDER_ERROR_BYTES + 1)),
        }
        for label, provider in cases.items():
            with self.subTest(case=label):
                self.assertEqual(_explained(provider), FALLBACK)

    def test_a_raising_call_says_so_rather_than_propagating(self):
        """The one case that adds to the sentence instead of replacing it."""
        explained = _explained(CallRaises())
        self.assertTrue(explained.startswith(FALLBACK), explained)
        self.assertIn("provider explanation unavailable", explained)
        self.assertIn("boom", explained)

    def test_the_name_comes_from_the_class_when_there_is_no_name(self):
        class Anonymous:
            pass

        self.assertEqual(_explained(Anonymous()), "Anonymous boundary changed")


class StringSubclassTests(unittest.TestCase):
    """OBL-PROVIDERS-051: a str subclass is a str, and can still lie."""

    def test_an_under_reported_length_alone_does_not_get_through(self):
        """Held, but by accident: str.strip returns a plain str.

        The helper strips before it measures, and str.strip on a subclass
        returns a plain str, so the overridden encode is never the one that
        runs. A fix that moved the measurement before the strip, or dropped
        the strip, would open this.
        """
        lying = LyingEncode("y" * (MAX_PROVIDER_ERROR_BYTES + 5000))
        self.assertEqual(_explained(_returning(lying)), FALLBACK)
        self.assertIs(type(lying.strip()), str)

    def test_a_subclass_that_resists_laundering_is_still_measured(self):
        """Known divergence: overriding strip as well bypasses the ceiling."""
        lying = LyingBoth("z" * (MAX_PROVIDER_ERROR_BYTES + 5000))
        self.assertEqual(_explained(_returning(lying)), FALLBACK)

    def test_a_raising_strip_yields_the_fallback(self):
        """Known divergence: it propagates out of the helper."""
        self.assertEqual(_explained(_returning(StripRaises("a"))), FALLBACK)

    def test_a_strip_returning_a_non_string_yields_the_fallback(self):
        """Known divergence: AttributeError from the encode that follows."""
        self.assertEqual(
            _explained(_returning(StripReturnsNonString("a"))), FALLBACK
        )


class StringSubclassScopeTests(unittest.TestCase):
    """Exactly what escapes, so a partial fix cannot pass unnoticed."""

    def test_a_string_subclass_cannot_bypass_the_ceiling(self):
        size = MAX_PROVIDER_ERROR_BYTES + 5000
        returned = _explained(_returning(LyingBoth("z" * size)))
        self.assertEqual(returned, FALLBACK)

    def test_misbehaving_string_subclasses_get_the_fallback(self):
        self.assertEqual(_explained(_returning(StripRaises("a"))), FALLBACK)
        self.assertEqual(_explained(_returning(StripReturnsNonString("a"))), FALLBACK)

    def test_a_plain_string_at_the_ceiling_is_accepted(self):
        """The contrast: the ceiling is not refusing everything near it."""
        exact = "x" * MAX_PROVIDER_ERROR_BYTES
        self.assertEqual(_explained(_returning(exact)), exact)
        self.assertEqual(
            _explained(_returning("x" * (MAX_PROVIDER_ERROR_BYTES + 1))), FALLBACK
        )


#: Explanations that pass the byte ceiling and then grow on the way out.
#: Each encodes to at most the ceiling with errors="replace", which is how
#: the helper measures them.
HOSTILE = {
    "lone surrogates": "\ud800" * 16000,
    "C0 controls": "\x01" * 16000,
    "astral characters": "\U0001f600" * 4000,
}

#: The obligation asks for the rendered size to stay within a documented
#: multiple of the ceiling. Nothing documents one, so the test picks the
#: smallest multiple that is not simply the ceiling itself and says so here.
RENDERED_ALLOWANCE = 2 * MAX_PROVIDER_ERROR_BYTES


def _terminal_line(explanation: str) -> str:
    """The bytes the `Provider detail:` line actually puts on the terminal."""
    stream = io.StringIO()
    with redirect_stdout(stream):
        output.safe_print(f"Provider detail: {explanation}")
    return stream.getvalue()


def _json_document(explanation: str) -> str:
    return _bounded_json_dumps({"provider_detail": explanation}, sort_keys=True)


class RenderedSizeTests(unittest.TestCase):
    """OBL-PROVIDERS-052: the ceiling bounds what goes in, not what comes out."""

    def test_render_expanding_explanations_are_refused(self):
        for label, candidate in HOSTILE.items():
            with self.subTest(case=label):
                self.assertEqual(_explained(_returning(candidate)), FALLBACK)

    def test_an_ordinary_explanation_renders_at_its_own_size(self):
        """The contrast: expansion is a property of the content, not the path."""
        plain = "x" * 16000
        self.assertLess(len(_terminal_line(plain)), RENDERED_ALLOWANCE)
        self.assertLess(len(_json_document(plain)), RENDERED_ALLOWANCE)

    def test_every_accepted_explanation_renders_within_the_allowance(self):
        """Known divergence: escaping multiplies both sinks."""
        for label, candidate in HOSTILE.items():
            for sink, render in (
                ("terminal", _terminal_line), ("json", _json_document)
            ):
                with self.subTest(case=label, sink=sink):
                    accepted = _explained(_returning(candidate))
                    self.assertLessEqual(len(render(accepted)), RENDERED_ALLOWANCE)

    def test_refused_explanations_render_as_the_bounded_fallback(self):
        measured = {
            (label, sink): len(render(_explained(_returning(candidate))))
            for label, candidate in HOSTILE.items()
            for sink, render in (
                ("terminal", _terminal_line), ("json", _json_document)
            )
        }
        self.assertTrue(all(size < 100 for size in measured.values()), measured)

    def test_an_explanation_of_newlines_never_reaches_a_sink(self):
        """The one hostile shape the ceiling does stop, and not by its size."""
        self.assertEqual(_explained(_returning("\n" * 16000)), FALLBACK)


if __name__ == "__main__":
    unittest.main()
