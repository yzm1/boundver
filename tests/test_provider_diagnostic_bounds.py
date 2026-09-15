"""Diagnostics about untrusted declarations, and how large they are allowed to be.

Two functions here exist to turn provider misbehaviour into error strings, and
both interpolate values the config controls into those strings. The error list
they return is bounded carefully - a count, a per-error byte ceiling measured
in bytes rather than characters, a whole-list discard on any bad element. The
values interpolated into the messages are not bounded at all, so the same
ceiling that refuses a 16,385-byte error from a provider will hand back a
100,537-character error about that provider's name.

The messages themselves are the third case: two ceilings are the same
number, so a value sitting on one of them cannot fit inside the other.

Covers OBL-PROVIDERS-034, OBL-PROVIDERS-042, OBL-PROVIDERS-045 and
OBL-PROVIDERS-046.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from boundver._utils import MAX_DECLARED_PATH_BYTES
from boundver.providers import (
    MAX_CUSTOM_PROVIDERS,
    MAX_PROVIDER_ENTRY_BYTES,
    MAX_PROVIDER_ERROR_BYTES,
    MAX_PROVIDER_ERRORS,
    MAX_PROVIDER_LABEL_BYTES,
    JsonCanonicalProvider,
    OpenApiCanonicalProvider,
    PathHashProvider,
    ProviderContext,
    ResolvedBoundary,
    _resolved_boundary_error,
    load_custom_providers,
    validate_provider_environment,
)

#: Long enough to be unmistakable and short enough to build in a test.
LONG = "m" * 100_000


def _provider(name="demo", returns=None, raises=None):
    """A provider whose environment hook does one specific thing."""

    class Demo:
        def validate_environment(self, boundary_cfg):
            if raises is not None:
                raise raises
            return returns

    Demo.name = name
    return Demo()


def _validated(provider):
    return validate_provider_environment(provider, {})


class Exploding:
    """A name object that refuses to be rendered."""

    def __format__(self, spec):
        raise RuntimeError("format exploded")

    def __str__(self):
        raise RuntimeError("str exploded")


class ErrorListBoundaryTests(unittest.TestCase):
    """OBL-PROVIDERS-046: every boundary, on the exact value."""

    def test_the_count_ceiling_is_inclusive(self):
        self.assertEqual(
            len(_validated(_provider(returns=["e"] * MAX_PROVIDER_ERRORS))),
            MAX_PROVIDER_ERRORS,
        )
        over = _validated(_provider(returns=["e"] * (MAX_PROVIDER_ERRORS + 1)))
        self.assertEqual(len(over), 1)
        self.assertIn(f"more than {MAX_PROVIDER_ERRORS} errors", over[0])

    def test_the_byte_ceiling_is_inclusive(self):
        exact = "x" * MAX_PROVIDER_ERROR_BYTES
        self.assertEqual(_validated(_provider(returns=[exact])), [exact])
        over = _validated(_provider(returns=[exact + "x"]))
        self.assertEqual(len(over), 1)
        self.assertIn(f"longer than {MAX_PROVIDER_ERROR_BYTES} bytes", over[0])

    def test_the_ceiling_is_measured_in_bytes_not_characters(self):
        for label, character in (("two-byte", "é"), ("three-byte", "一")):
            with self.subTest(width=label):
                wide = character * MAX_PROVIDER_ERROR_BYTES
                returned = _validated(_provider(returns=[wide]))
                self.assertEqual(len(returned), 1)
                self.assertIn("longer than", returned[0])

    def test_a_lone_surrogate_costs_one_byte(self):
        """Because the measurement replaces what it cannot encode."""
        surrogates = "\ud800" * MAX_PROVIDER_ERROR_BYTES
        self.assertEqual(_validated(_provider(returns=[surrogates])), [surrogates])

    def test_a_tuple_is_not_a_list(self):
        returned = _validated(_provider(returns=("a", "b")))
        self.assertEqual(len(returned), 1)
        self.assertIn("must return a list of errors", returned[0])

    def test_a_string_subclass_element_is_rejected(self):
        class Subclass(str):
            pass

        returned = _validated(_provider(returns=[Subclass("ok")]))
        self.assertEqual(len(returned), 1)
        self.assertIn("only non-empty error strings", returned[0])

    def test_one_bad_element_discards_every_good_one(self):
        returned = _validated(_provider(returns=["good"] * 99 + ["   "]))
        self.assertEqual(len(returned), 1)
        self.assertIn("only non-empty error strings", returned[0])

    def test_the_returned_list_is_a_distinct_object(self):
        shared = ["first"]
        returned = _validated(_provider(returns=shared))
        shared.append("added later")
        self.assertEqual(returned, ["first"])

    def test_a_missing_or_silent_hook_returns_nothing(self):
        class NotCallable:
            name = "demo"
            validate_environment = "not a function"

        self.assertEqual(_validated(NotCallable()), [])
        self.assertEqual(_validated(_provider(returns=None)), [])


class ProviderNameInMessageTests(unittest.TestCase):
    """OBL-PROVIDERS-045: the name is interpolated raw into five messages."""

    def test_an_ordinary_name_is_reported(self):
        """The premise: the name really does reach the message."""
        returned = _validated(_provider(name="demo", raises=ValueError("boom")))
        self.assertEqual(len(returned), 1)
        self.assertIn("Provider 'demo'", returned[0])

    def test_a_non_string_name_is_rendered_without_complaint(self):
        returned = _validated(_provider(name=12345, raises=ValueError("boom")))
        self.assertIn("Provider 'Demo'", returned[0])

    def test_a_name_that_raises_on_access_falls_back_to_the_class(self):
        class NameRaises:
            @property
            def name(self):
                raise RuntimeError("name exploded")

            def validate_environment(self, boundary_cfg):
                raise ValueError("boom")

        self.assertIn("Provider 'NameRaises'", _validated(NameRaises())[0])

    def test_a_long_name_does_not_produce_a_long_message(self):
        """Known divergence: the message is as long as the name."""
        returned = _validated(_provider(name=LONG, raises=ValueError("boom")))
        self.assertLessEqual(
            len(returned[0].encode("utf-8")), MAX_PROVIDER_ERROR_BYTES
        )

    def test_a_name_that_cannot_be_rendered_does_not_propagate(self):
        """Known divergence: __format__ raises straight out of the helper."""

        class NameExplodes:
            name = Exploding()

            def validate_environment(self, boundary_cfg):
                raise ValueError("boom")

        self.assertIsInstance(_validated(NameExplodes()), list)


class ProviderNameScopeTests(unittest.TestCase):
    """What each divergence costs, so a partial fix cannot pass unnoticed."""

    def test_the_message_including_the_name_is_bounded(self):
        returned = _validated(_provider(name=LONG, raises=ValueError("boom")))
        self.assertEqual(len(returned), 1)
        self.assertLessEqual(len(returned[0].encode("utf-8")), MAX_PROVIDER_ERROR_BYTES)

    def test_the_unrenderable_name_falls_back_to_the_class(self):
        class NameExplodes:
            name = Exploding()

            def validate_environment(self, boundary_cfg):
                raise ValueError("boom")

        self.assertIn("NameExplodes", _validated(NameExplodes())[0])

    def test_the_error_the_provider_returned_is_still_bounded(self):
        """The contrast: what the provider says is bounded, what we say is not."""
        returned = _validated(
            _provider(name=LONG, returns=["x" * (MAX_PROVIDER_ERROR_BYTES + 1)])
        )
        self.assertEqual(len(returned), 1)
        self.assertIn("longer than", returned[0])
        self.assertLessEqual(len(returned[0].encode("utf-8")), MAX_PROVIDER_ERROR_BYTES)


class CustomProviderLoaderTests(unittest.TestCase):
    """OBL-PROVIDERS-042: the same shape, in the loader."""

    def _loaded(self, declarations):
        return load_custom_providers(declarations, True, {})

    def test_an_ordinary_failure_is_short(self):
        """The premise: these messages are small when their inputs are."""
        errors = self._loaded([{"module": "mod", "class": "C", "name": "n"}])
        self.assertEqual(len(errors), 1)
        self.assertLess(len(errors[0]), 200)

    def test_a_long_module_name_does_not_produce_a_long_error(self):
        """Known divergence: it is interpolated whole."""
        errors = self._loaded([{"module": LONG, "class": "C", "name": "n"}])
        self.assertLessEqual(
            len(errors[0].encode("utf-8")), MAX_PROVIDER_ERROR_BYTES
        )

    def test_the_whole_list_stays_within_one_ceiling_per_declaration(self):
        """Known divergence: it scales with the declarations."""
        declarations = [
            {"module": LONG, "class": LONG, "name": f"n{index}"}
            for index in range(10)
        ]
        errors = self._loaded(declarations)
        self.assertLessEqual(
            sum(len(error) for error in errors),
            len(declarations) * MAX_PROVIDER_ERROR_BYTES,
        )

    def test_one_declaration_stays_within_one_error_ceiling(self):
        errors = self._loaded([{"module": LONG, "class": "C", "name": "n"}])
        self.assertEqual(len(errors), 1)
        self.assertLessEqual(len(errors[0].encode("utf-8")), MAX_PROVIDER_ERROR_BYTES)

    def test_the_declaration_count_itself_is_bounded(self):
        """The contrast: the loader does bound the thing it was asked to."""
        errors = self._loaded(
            [{"module": "m", "class": "C", "name": f"n{index}"}
             for index in range(MAX_CUSTOM_PROVIDERS + 1)]
        )
        self.assertEqual(len(errors), 1)
        self.assertIn(f"{MAX_CUSTOM_PROVIDERS}-provider limit", errors[0])


#: A declared path just under the ceiling boundver accepts for one.
LONG_PATH = "p" * (MAX_DECLARED_PATH_BYTES - 10)


def _context(name: str, *, raise_on_read: bool, content: bytes = b"{}"):
    """A provider context whose single file is named *name* under svc/."""
    repo_relative = f"svc/{name}"

    def read_file(path: str) -> bytes:
        if raise_on_read:
            raise OSError("read exploded")
        return content

    def read_file_limited(path: str, max_bytes: int) -> bytes:
        return read_file(path)

    def list_files(prefix: str):
        return [repo_relative]

    return ProviderContext(
        repo_root=Path("."),
        component_path="svc",
        boundary_cfg={"paths": [name]},
        source="head",
        read_file=read_file,
        read_file_limited=read_file_limited,
        list_files=list_files,
    )


#: Each built-in provider, and a way to make it fail on one file.
FAILING_RESOLUTIONS = {
    "path-hash": (PathHashProvider(), {"raise_on_read": True}),
    "json-canonical": (
        JsonCanonicalProvider(), {"raise_on_read": False, "content": b"{"}
    ),
    "openapi-canonical": (
        OpenApiCanonicalProvider(), {"raise_on_read": False, "content": b"{"}
    ),
}


class ProviderErrorLengthTests(unittest.TestCase):
    """OBL-PROVIDERS-034: the ceiling a message enforces must bind the message."""

    def _resolved_errors(self, provider, kwargs, name):
        resolved = provider.resolve(_context(name, **kwargs))
        self.assertTrue(resolved.errors, resolved.status)
        return resolved.errors

    def test_an_ordinary_path_produces_a_short_error(self):
        """The premise: these messages are small when their inputs are."""
        for label, (provider, kwargs) in FAILING_RESOLUTIONS.items():
            with self.subTest(provider=label):
                errors = self._resolved_errors(provider, kwargs, "api.json")
                self.assertLess(len(errors[0]), 200, errors[0])

    def test_a_long_path_does_not_push_the_error_over_the_ceiling(self):
        """Known divergence: the path is interpolated whole."""
        for label, (provider, kwargs) in FAILING_RESOLUTIONS.items():
            with self.subTest(provider=label):
                errors = self._resolved_errors(provider, kwargs, LONG_PATH)
                self.assertLessEqual(
                    len(errors[0].encode("utf-8")), MAX_PROVIDER_ERROR_BYTES
                )

    def test_the_validators_own_message_stays_within_its_own_ceiling(self):
        """Known divergence: a label at its ceiling overflows the error one."""
        label = "a" * MAX_PROVIDER_LABEL_BYTES
        message = _resolved_boundary_error(
            ResolvedBoundary(entries=[(label, b"x" * (MAX_PROVIDER_ENTRY_BYTES + 1))])
        )
        self.assertIsNotNone(message)
        self.assertLessEqual(
            len(message.encode("utf-8")), MAX_PROVIDER_ERROR_BYTES
        )

    def test_every_message_stays_within_the_ceiling(self):
        measured = {
            label: len(self._resolved_errors(provider, kwargs, LONG_PATH)[0])
            for label, (provider, kwargs) in FAILING_RESOLUTIONS.items()
        }
        self.assertTrue(
            all(size <= MAX_PROVIDER_ERROR_BYTES for size in measured.values()),
            measured,
        )
        label = "a" * MAX_PROVIDER_LABEL_BYTES
        validator = _resolved_boundary_error(
            ResolvedBoundary(entries=[(label, b"x" * (MAX_PROVIDER_ENTRY_BYTES + 1))])
        )
        self.assertLessEqual(len(validator.encode("utf-8")), MAX_PROVIDER_ERROR_BYTES)

    def test_both_ceilings_are_the_same_number(self):
        """Which is why a value at one of them cannot fit inside the other."""
        self.assertEqual(MAX_DECLARED_PATH_BYTES, MAX_PROVIDER_ERROR_BYTES)
        self.assertEqual(MAX_PROVIDER_LABEL_BYTES, MAX_PROVIDER_ERROR_BYTES)


if __name__ == "__main__":
    unittest.main()
