"""Adversarial tests for provider inputs and canonical-provider parsing."""

import json
import math
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import boundver.providers as providers
from boundver._hashing import _ModeAwareBytes
from boundver._utils import ProviderError
from boundver.providers import (
    ImplicitProvider,
    JsonCanonicalProvider,
    LeafProvider,
    OpenApiCanonicalProvider,
    ProviderContext,
    ResolvedBoundary,
    compute_boundary,
    register_provider,
    load_custom_providers,
)


def _context(raw=b"{}", *, filename="contract.json", provider="custom.test"):
    path = f"svc/{filename}"
    files = {path: raw}
    return ProviderContext(
        repo_root=Path("/repo"),
        component_path="svc",
        boundary_cfg={"provider": provider, "paths": [filename]},
        source="working-tree",
        read_file=lambda repo_path: files[repo_path],
        list_files=lambda prefix: sorted(
            repo_path
            for repo_path in files
            if repo_path == prefix or repo_path.startswith(prefix.rstrip("/") + "/")
        ),
    )


class _Provider:
    name = "custom.test"
    version = "1"

    def __init__(self, result):
        self.result = result

    def resolve(self, ctx):
        return self.result


class TestProviderObjectContract(unittest.TestCase):
    def test_registration_requires_name_version_and_callable_resolve(self):
        cases = [
            type("MissingName", (), {"version": "1", "resolve": lambda self, ctx: None})(),
            type("MissingVersion", (), {"name": "custom.x", "resolve": lambda self, ctx: None})(),
            type("MissingResolve", (), {"name": "custom.x", "version": "1"})(),
            type(
                "BlankVersion",
                (),
                {"name": "custom.x", "version": " ", "resolve": lambda self, ctx: None},
            )(),
        ]
        for provider in cases:
            with self.subTest(provider=type(provider).__name__):
                with self.assertRaises(ProviderError):
                    register_provider(provider, registry={})

    def test_resolve_exception_becomes_controlled_error(self):
        class Broken:
            name = "custom.broken"
            version = "1"

            def resolve(self, ctx):
                raise RuntimeError("parser exploded")

        result = compute_boundary(Broken(), _context())
        self.assertEqual(result[0:2], (None, "error"))
        self.assertIn("resolve() failed", result[2][0])
        self.assertIn("parser exploded", result[2][0])

    def test_invalid_identity_becomes_controlled_error(self):
        class MissingVersion:
            name = "custom.broken"

            def resolve(self, ctx):
                return ResolvedBoundary()

        digest, status, errors = compute_boundary(MissingVersion(), _context())
        self.assertIsNone(digest)
        self.assertEqual(status, "error")
        self.assertIn("Invalid boundary provider", errors[0])

    def test_custom_loader_cross_checks_configured_runtime_name(self):
        module_name = "_boundver_hardening_name_mismatch"
        module = types.ModuleType(module_name)

        class NamedProvider:
            name = "custom.runtime"
            version = "1"

            def resolve(self, ctx):
                return ResolvedBoundary(entries=[("contract", b"value")])

        module.NamedProvider = NamedProvider
        sys.modules[module_name] = module
        try:
            registry = {}
            errors = load_custom_providers(
                [
                    {
                        "module": module_name,
                        "class": "NamedProvider",
                        "name": "custom.configured",
                    }
                ],
                allow_custom=True,
                registry=registry,
            )
            self.assertEqual(registry, {})
            self.assertTrue(any("does not match configured" in error for error in errors))
        finally:
            del sys.modules[module_name]

    def test_custom_loader_rejects_duplicate_runtime_name(self):
        module_name = "_boundver_hardening_duplicate_name"
        module = types.ModuleType(module_name)

        class FirstProvider:
            name = "custom.duplicate"
            version = "1"

            def resolve(self, ctx):
                return ResolvedBoundary(entries=[("contract", b"first")])

        class SecondProvider(FirstProvider):
            def resolve(self, ctx):
                return ResolvedBoundary(entries=[("contract", b"second")])

        module.FirstProvider = FirstProvider
        module.SecondProvider = SecondProvider
        sys.modules[module_name] = module
        try:
            registry = {}
            errors = load_custom_providers(
                [
                    {"module": module_name, "class": "FirstProvider"},
                    {"module": module_name, "class": "SecondProvider"},
                ],
                allow_custom=True,
                registry=registry,
            )
            self.assertIsInstance(registry["custom.duplicate"], FirstProvider)
            self.assertTrue(any("Duplicate custom provider name" in error for error in errors))
        finally:
            del sys.modules[module_name]

    def test_custom_loader_does_not_replace_existing_registration(self):
        module_name = "_boundver_hardening_existing_name"
        module = types.ModuleType(module_name)

        class Replacement:
            name = "custom.existing"
            version = "1"

            def resolve(self, ctx):
                return ResolvedBoundary(entries=[("contract", b"new")])

        module.Replacement = Replacement
        sys.modules[module_name] = module
        existing = _Provider(ResolvedBoundary(entries=[("contract", b"old")]))
        existing.name = "custom.existing"
        registry = {existing.name: existing}
        try:
            errors = load_custom_providers(
                [{"module": module_name, "class": "Replacement"}],
                allow_custom=True,
                registry=registry,
            )
            self.assertIs(registry["custom.existing"], existing)
            self.assertTrue(any("already registered" in error for error in errors))
        finally:
            del sys.modules[module_name]


class TestResolvedBoundaryContract(unittest.TestCase):
    def _error_for(self, result):
        digest, status, errors = compute_boundary(_Provider(result), _context())
        self.assertIsNone(digest)
        self.assertEqual(status, "error")
        self.assertTrue(errors)
        return errors[0]

    def test_requires_exact_resolved_boundary_type(self):
        class Subclass(ResolvedBoundary):
            pass

        self.assertIn("exactly ResolvedBoundary", self._error_for(Subclass()))
        self.assertIn("exactly ResolvedBoundary", self._error_for({"entries": []}))

    def test_status_and_errors_are_consistent(self):
        cases = [
            (ResolvedBoundary(status="unknown"), "status must be"),
            (ResolvedBoundary(errors=["warning"]), "cannot include errors"),
            (ResolvedBoundary(status="error"), "requires at least one error"),
            (ResolvedBoundary(status="partial"), "requires at least one error"),
            (
                ResolvedBoundary(status="error", errors=["bad"], entries=[("a", b"x")]),
                "cannot include hash entries",
            ),
            (ResolvedBoundary(errors=[""]), "non-empty strings"),
        ]
        for result, expected in cases:
            with self.subTest(expected=expected):
                self.assertIn(expected, self._error_for(result))

    def test_entries_require_exact_types_and_sorted_unique_labels(self):
        cases = [
            (ResolvedBoundary(entries=(('a', b'x'),)), "entries must be a list"),
            (ResolvedBoundary(entries=[["a", b"x"]]), "exact two-item tuples"),
            (ResolvedBoundary(entries=[("", b"x")]), "non-empty strings"),
            (ResolvedBoundary(entries=[("a", bytearray(b"x"))]), "must be bytes"),
            (
                ResolvedBoundary(entries=[("b", b"x"), ("a", b"x")]),
                "sorted order",
            ),
            (
                ResolvedBoundary(entries=[("a", b"x"), ("a", b"y")]),
                "sorted order",
            ),
        ]
        for result, expected in cases:
            with self.subTest(expected=expected):
                self.assertIn(expected, self._error_for(result))

    def test_mode_aware_source_bytes_are_preserved_for_raw_provider_hashing(self):
        regular = ResolvedBoundary(
            entries=[("contract", _ModeAwareBytes(b"target", "100644"))]
        )
        executable = ResolvedBoundary(
            entries=[("contract", _ModeAwareBytes(b"target", "100755"))]
        )
        first = compute_boundary(_Provider(regular), _context())
        second = compute_boundary(_Provider(executable), _context())
        self.assertEqual(first[1], "ok")
        self.assertEqual(second[1], "ok")
        self.assertNotEqual(first[0], second[0])

    def test_raw_provider_labels_preserve_surrogateescaped_git_filename_bytes(self):
        result = ResolvedBoundary(entries=[("file:\udcff.bin", b"content")])
        digest, status, errors = compute_boundary(_Provider(result), _context())
        self.assertIsNotNone(digest)
        self.assertEqual(status, "ok")
        self.assertEqual(errors, [])

        invalid = ResolvedBoundary(entries=[("file:\ud800.bin", b"content")])
        error = self._error_for(invalid)
        self.assertIn("surrogateescaped Git bytes", error)

    def test_entry_count_and_byte_limits_are_enforced(self):
        with patch.object(providers, "MAX_PROVIDER_ENTRIES", 1):
            error = self._error_for(
                ResolvedBoundary(entries=[("a", b"x"), ("b", b"y")])
            )
            self.assertIn("1-item limit", error)
        with patch.object(providers, "MAX_PROVIDER_ENTRY_BYTES", 1):
            error = self._error_for(ResolvedBoundary(entries=[("a", b"xx")]))
            self.assertIn("1-byte limit", error)
        with patch.object(providers, "MAX_PROVIDER_TOTAL_BYTES", 1):
            error = self._error_for(
                ResolvedBoundary(entries=[("a", b"x"), ("b", b"y")])
            )
            self.assertIn("aggregate limit", error)
        with patch.object(providers, "MAX_PROVIDER_TOTAL_LABEL_BYTES", 1):
            error = self._error_for(ResolvedBoundary(entries=[("aa", b"x")]))
            self.assertIn("entry labels", error)

    def test_metadata_must_be_bounded_json(self):
        invalid_metadata = [
            ["not", "an", "object"],
            {"bad": object()},
            {"bad": math.nan},
            {1: "non-string-key"},
        ]
        cycle = {}
        cycle["self"] = cycle
        invalid_metadata.append(cycle)
        for metadata in invalid_metadata:
            with self.subTest(metadata_type=type(metadata).__name__):
                error = self._error_for(
                    ResolvedBoundary(entries=[("a", b"x")], metadata=metadata)
                )
                self.assertIn("metadata", error)

        with patch.object(providers, "MAX_PROVIDER_METADATA_BYTES", 8):
            error = self._error_for(
                ResolvedBoundary(entries=[("a", b"x")], metadata={"value": "long"})
            )
            self.assertIn("JSON limit", error)

    def test_only_explicit_builtin_null_boundaries_are_sanctioned(self):
        error = self._error_for(ResolvedBoundary())
        self.assertIn("without publishing entries", error)

        self.assertEqual(
            compute_boundary(LeafProvider(), _context()),
            (None, "ok", []),
        )
        implicit = compute_boundary(
            ImplicitProvider(),
            ProviderContext(
                repo_root=Path("/repo"),
                component_path="svc",
                boundary_cfg={},
                source="head",
                read_file=lambda path: b"",
                list_files=lambda prefix: [],
            ),
        )
        self.assertEqual(implicit[0:2], (None, "partial"))


class TestJsonCanonicalHardening(unittest.TestCase):
    def _resolve(self, raw):
        return JsonCanonicalProvider().resolve(
            _context(raw, filename="schema.json", provider="json-canonical")
        )

    def test_rejects_duplicate_keys(self):
        result = self._resolve(b'{"role":"user","role":"admin"}')
        self.assertEqual(result.status, "error")
        self.assertIn("duplicate object key", result.errors[0])

    def test_rejects_nan_and_infinity(self):
        for token in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(token=token):
                result = self._resolve(b'{"value":' + token + b"}")
                self.assertEqual(result.status, "error")
                self.assertIn("non-finite", result.errors[0])

    def test_rejects_oversized_json_integers(self):
        result = self._resolve(b'{"value":' + b"9" * 5000 + b"}")
        self.assertEqual(result.status, "error")
        self.assertIn("decimal-digit limit", result.errors[0])

    def test_rejects_escaped_lone_surrogate_without_raising(self):
        result = self._resolve(b'{"value":"\\ud800"}')
        self.assertEqual(result.status, "error")
        self.assertIn("valid Unicode/UTF-8", result.errors[0])

    def test_provider_version_marks_stricter_canonical_contract(self):
        self.assertEqual(JsonCanonicalProvider.version, "2")

    def test_semantic_canonicalization_intentionally_drops_file_mode(self):
        raw = b'{"contract":true}'
        contexts = []
        for mode in ("100644", "100755"):
            context = _context(raw, filename="schema.json", provider="json-canonical")
            context.read_file = lambda path, mode=mode: _ModeAwareBytes(raw, mode)
            contexts.append(context)
        first = compute_boundary(JsonCanonicalProvider(), contexts[0])
        second = compute_boundary(JsonCanonicalProvider(), contexts[1])
        self.assertEqual(first[0], second[0])


class TestOpenApiCanonicalHardening(unittest.TestCase):
    def _resolve(self, raw, filename="openapi.yaml"):
        return OpenApiCanonicalProvider().resolve(
            _context(raw, filename=filename, provider="openapi-canonical")
        )

    def _canonical(self, raw, filename="openapi.yaml"):
        result = self._resolve(raw, filename)
        self.assertEqual(result.status, "ok", result.errors)
        return json.loads(result.entries[0][1].decode("utf-8"))

    def test_validates_document_root_and_version(self):
        cases = [
            (b"[]", "root must be an object"),
            (b"paths: {}", "must declare 'openapi'"),
            (b"openapi: 2.0.0\npaths: {}", "expected an OpenAPI 3.0.x"),
            (b"openapi: 3.1.0\nswagger: '2.0'\npaths: {}", "must not declare both"),
            (b"openapi: null\nswagger: '2.0'\npaths: {}", "must not declare both"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                result = self._resolve(raw)
                self.assertEqual(result.status, "error")
                self.assertIn(expected, result.errors[0])

    def test_rejects_duplicate_json_and_yaml_keys(self):
        json_result = self._resolve(
            b'{"openapi":"3.1.0","paths":{},"paths":{"/admin":{}}}',
            "openapi.json",
        )
        self.assertEqual(json_result.status, "error")
        self.assertIn("duplicate object key", json_result.errors[0])

        yaml_result = self._resolve(
            b"openapi: 3.1.0\npaths: {}\npaths:\n  /admin: {}\n"
        )
        self.assertEqual(yaml_result.status, "error")
        self.assertIn("duplicate mapping key", yaml_result.errors[0])

    def test_rejects_oversized_json_integers(self):
        result = self._resolve(
            b'{"openapi":"3.1.0","paths":{},"x-value":'
            + b"9" * 5000
            + b"}",
            "openapi.json",
        )
        self.assertEqual(result.status, "error")
        self.assertIn("decimal-digit limit", result.errors[0])

    def test_rejects_escaped_lone_surrogate_without_raising(self):
        for raw in (
            b'{"openapi":"3.1.0","paths":{},"x-value":"\\ud800"}',
            b'{"openapi":"3.1.0","paths":{},"\\ud800":true}',
        ):
            with self.subTest(raw=raw):
                result = self._resolve(raw, "openapi.json")
                self.assertEqual(result.status, "error")
                self.assertIn("valid Unicode/UTF-8", result.errors[0])

    def test_numeric_or_mixed_response_keys_fail_cleanly(self):
        result = self._resolve(
            b"""openapi: 3.1.0
paths:
  /items:
    get:
      responses:
        200: {}
        "200": {}
"""
        )
        self.assertEqual(result.status, "error")
        self.assertIn("quote numeric response keys", result.errors[0])

    def test_yaml_12_boolean_semantics_and_extensions_are_retained(self):
        canonical = self._canonical(
            b"""openapi: 3.1.0
paths: {}
x-on: on
x-enabled: true
"""
        )
        self.assertEqual(canonical["x-on"], "on")
        self.assertIs(canonical["x-enabled"], True)

    def test_yaml_11_numeric_spellings_do_not_collapse_into_decimal_values(self):
        cases = (("012", "10"), ("1:20", "80"))
        for ambiguous, decimal in cases:
            with self.subTest(ambiguous=ambiguous):
                first = compute_boundary(
                    OpenApiCanonicalProvider(),
                    _context(
                        (
                            "openapi: 3.1.0\npaths: {}\n"
                            f"x-default: {ambiguous}\n"
                        ).encode(),
                        filename="api.yaml",
                    ),
                )
                second = compute_boundary(
                    OpenApiCanonicalProvider(),
                    _context(
                        (
                            "openapi: 3.1.0\npaths: {}\n"
                            f"x-default: {decimal}\n"
                        ).encode(),
                        filename="api.yaml",
                    ),
                )
                self.assertEqual(first[1], "ok", first[2])
                self.assertEqual(second[1], "ok", second[2])
                self.assertNotEqual(first[0], second[0])

    def test_extension_change_changes_digest(self):
        before = b'openapi: 3.1.0\npaths: {}\nx-generator-mode: strict\n'
        after = b'openapi: 3.1.0\npaths: {}\nx-generator-mode: loose\n'
        first = compute_boundary(OpenApiCanonicalProvider(), _context(before, filename="api.yaml"))
        second = compute_boundary(OpenApiCanonicalProvider(), _context(after, filename="api.yaml"))
        self.assertEqual(first[1], "ok")
        self.assertEqual(second[1], "ok")
        self.assertNotEqual(first[0], second[0])

    def test_rejects_unsafe_scalars_and_aliases(self):
        cases = [
            b"openapi: 3.1.0\npaths: {}\nx-value: .nan\n",
            b"openapi: 3.1.0\npaths: {}\nx-data: !!binary aGVsbG8=\n",
            b"openapi: 3.1.0\npaths: &routes {}\nx-copy: *routes\n",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                result = self._resolve(raw)
                self.assertEqual(result.status, "error")

    def test_rejects_external_and_local_file_references(self):
        for ref in ("https://example.test/schema.yaml#/User", "models.yaml#/User"):
            with self.subTest(ref=ref):
                raw = json.dumps(
                    {
                        "openapi": "3.1.0",
                        "paths": {},
                        "components": {"schemas": {"User": {"$ref": ref}}},
                    }
                ).encode("utf-8")
                result = self._resolve(raw, "api.json")
                self.assertEqual(result.status, "error")
                self.assertIn("only same-document fragment", result.errors[0])

        valid = self._resolve(
            b'{"openapi":"3.1.0","paths":{},"components":{"schemas":'
            b'{"User":{"type":"object"},"Alias":{"$ref":"#/components/schemas/User"}}}}',
            "api.json",
        )
        self.assertEqual(valid.status, "ok", valid.errors)

    def test_json_extension_is_not_silently_reparsed_as_yaml(self):
        result = self._resolve(b"openapi: 3.1.0\npaths: {}\n", "api.json")
        self.assertEqual(result.status, "error")
        self.assertIn("JSON parse failed", result.errors[0])

    def test_provider_version_marks_hardened_contract(self):
        self.assertEqual(OpenApiCanonicalProvider.version, "3")


if __name__ == "__main__":
    unittest.main()
