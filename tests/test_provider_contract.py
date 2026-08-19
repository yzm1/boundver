"""Focused regression tests for the public provider contract."""

import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

from boundver._config import validate_config
from boundver._hashing import HASH_DOMAIN_BOUNDARY, _hash_framed_entries
from boundver._lockfile import generate_lockfile
from boundver.providers import (
    OpenApiCanonicalProvider,
    ProviderContext,
    ResolvedBoundary,
    compute_boundary,
    create_registry,
    explain_provider_diff,
    get_provider,
    load_custom_providers,
    register_provider,
    validate_provider_config,
)


ROOT = Path("/repo")


def _context(document=None):
    files = {}
    if document is not None:
        files["svc/openapi.json"] = json.dumps(document).encode("utf-8")

    return ProviderContext(
        repo_root=ROOT,
        component_path="svc",
        boundary_cfg={"paths": ["openapi.json"]},
        source="working-tree",
        read_file=lambda path: files[path],
        list_files=lambda prefix: sorted(
            path for path in files if path == prefix or path.startswith(prefix + "/")
        ),
    )


def _openapi_digest(document):
    digest, status, errors = compute_boundary(
        OpenApiCanonicalProvider(), _context(document)
    )
    if status != "ok" or errors:
        raise AssertionError((status, errors))
    return digest


class TestOpenApiNamedMaps(unittest.TestCase):
    def test_annotation_looking_property_names_are_contract(self):
        before = {
            "openapi": "3.1.0",
            "paths": {},
            "components": {
                "schemas": {
                    "Record": {
                        "type": "object",
                        "properties": {
                            "description": {
                                "type": "string",
                                "description": "documentation is ignored",
                            },
                            "example": {"type": "string"},
                            "x-internal": {"type": "boolean"},
                        },
                    }
                }
            },
        }
        after = json.loads(json.dumps(before))
        after["components"]["schemas"]["Record"]["properties"]["description"][
            "type"
        ] = "integer"

        self.assertNotEqual(_openapi_digest(before), _openapi_digest(after))

        resolved = OpenApiCanonicalProvider().resolve(_context(before))
        canonical = json.loads(resolved.entries[0][1].decode("utf-8"))
        properties = canonical["components"]["schemas"]["Record"]["properties"]
        self.assertEqual(
            set(properties), {"description", "example", "x-internal"}
        )
        self.assertNotIn("description", properties["description"])

    def test_annotation_looking_component_schema_names_are_contract(self):
        before = {
            "openapi": "3.1.0",
            "paths": {},
            "components": {
                "schemas": {
                    "description": {"type": "string"},
                    "example": {"type": "number"},
                    "x-private": {"type": "boolean"},
                }
            },
        }
        after = json.loads(json.dumps(before))
        after["components"]["schemas"]["x-private"]["type"] = "integer"

        self.assertNotEqual(_openapi_digest(before), _openapi_digest(after))

        resolved = OpenApiCanonicalProvider().resolve(_context(before))
        canonical = json.loads(resolved.entries[0][1].decode("utf-8"))
        self.assertEqual(
            set(canonical["components"]["schemas"]),
            {"description", "example", "x-private"},
        )

    def test_annotation_looking_definition_names_are_contract(self):
        before = {
            "swagger": "2.0",
            "paths": {},
            "definitions": {
                "description": {"type": "string"},
                "x-private": {"type": "boolean"},
            },
        }
        after = json.loads(json.dumps(before))
        after["definitions"]["description"]["type"] = "integer"
        self.assertNotEqual(_openapi_digest(before), _openapi_digest(after))

    def test_documentation_changes_are_ignored_but_extensions_are_contract(self):
        before = {
            "openapi": "3.1.0",
            "paths": {
                "/widgets": {
                    "description": "old docs",
                    "get": {
                        "summary": "old summary",
                        "x-internal": True,
                        "responses": {"204": {"description": "No content"}},
                    },
                }
            },
        }
        after = json.loads(json.dumps(before))
        after["paths"]["/widgets"]["description"] = "new docs"
        after["paths"]["/widgets"]["get"]["summary"] = "new summary"
        after["paths"]["/widgets"]["get"]["x-internal"] = False
        self.assertNotEqual(_openapi_digest(before), _openapi_digest(after))

        docs_only = json.loads(json.dumps(before))
        docs_only["paths"]["/widgets"]["description"] = "new docs"
        docs_only["paths"]["/widgets"]["get"]["summary"] = "new summary"
        self.assertEqual(_openapi_digest(before), _openapi_digest(docs_only))

    def test_annotation_named_callback_link_and_variable_entries_are_contract(self):
        document = {
            "openapi": "3.1.0",
            "paths": {
                "/widgets": {
                    "post": {
                        "callbacks": {
                            "description": {
                                "{$request.body#/callbackUrl}": {
                                    "post": {
                                        "operationId": "descriptionCallback",
                                        "responses": {
                                            "204": {"description": "received"}
                                        },
                                    }
                                }
                            },
                            "x-partner": {
                                "{$request.body#/partnerUrl}": {
                                    "post": {
                                        "operationId": "partnerCallback",
                                        "responses": {
                                            "204": {"description": "received"}
                                        },
                                    }
                                }
                            },
                        },
                        "responses": {
                            "200": {
                                "description": "success",
                                "links": {
                                    "description": {
                                        "operationId": "getDescription"
                                    },
                                    "x-related": {"operationId": "getRelated"},
                                },
                            }
                        },
                        "servers": [
                            {
                                "url": "https://{description}.{x-region}.example.test",
                                "variables": {
                                    "description": {"default": "api"},
                                    "x-region": {"default": "us"},
                                },
                            }
                        ],
                    }
                }
            },
        }
        resolved = OpenApiCanonicalProvider().resolve(_context(document))
        canonical = json.loads(resolved.entries[0][1].decode("utf-8"))
        operation = canonical["paths"]["/widgets"]["post"]

        self.assertEqual(
            set(operation["callbacks"]), {"description", "x-partner"}
        )
        self.assertEqual(
            set(operation["responses"]["200"]["links"]),
            {"description", "x-related"},
        )
        self.assertEqual(
            set(operation["servers"][0]["variables"]),
            {"description", "x-region"},
        )

        mutations = [
            (
                ("paths", "/widgets", "post", "callbacks", "x-partner",
                 "{$request.body#/partnerUrl}", "post", "operationId"),
                "changedPartnerCallback",
            ),
            (
                ("paths", "/widgets", "post", "responses", "200", "links",
                 "description", "operationId"),
                "changedDescriptionLink",
            ),
            (
                ("paths", "/widgets", "post", "servers", 0, "variables",
                 "x-region", "default"),
                "eu",
            ),
        ]
        baseline_digest = _openapi_digest(document)
        for path, value in mutations:
            with self.subTest(path=path):
                changed = json.loads(json.dumps(document))
                target = changed
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = value
                self.assertNotEqual(baseline_digest, _openapi_digest(changed))


class TestBoundaryComputationContract(unittest.TestCase):
    class MetadataProvider:
        name = "custom.metadata-test"
        version = "1"

        def resolve(self, ctx):
            return ResolvedBoundary(
                entries=[("contract", b"value")],
                metadata={"symbols": ["Widget"]},
            )

    def test_default_return_shape_remains_three_items(self):
        result = compute_boundary(self.MetadataProvider(), _context())
        self.assertEqual(len(result), 3)
        self.assertEqual(
            result[0],
            _hash_framed_entries(
                [("contract", b"value")], domain=HASH_DOMAIN_BOUNDARY
            ),
        )

    def test_metadata_can_be_requested_without_affecting_digest(self):
        ordinary = compute_boundary(self.MetadataProvider(), _context())
        with_metadata = compute_boundary(
            self.MetadataProvider(), _context(), include_metadata=True
        )
        self.assertEqual(with_metadata[:3], ordinary)
        self.assertEqual(with_metadata[3], {"symbols": ["Widget"]})

    def test_metadata_is_available_for_an_error_resolution(self):
        class ErrorProvider(self.MetadataProvider):
            def resolve(self, ctx):
                return ResolvedBoundary(
                    status="error",
                    errors=["invalid contract"],
                    metadata={"parser": "v2"},
                )

        self.assertEqual(
            compute_boundary(ErrorProvider(), _context(), include_metadata=True),
            (None, "error", ["invalid contract"], {"parser": "v2"}),
        )


class TestProviderRegistryIsolation(unittest.TestCase):
    class IsolatedProvider:
        name = "custom.provider-contract-isolated"
        version = "1"

        def resolve(self, ctx):
            return ResolvedBoundary(entries=[("contract", b"value")])

    def test_registration_can_be_scoped_to_one_registry(self):
        first = create_registry()
        second = create_registry()
        provider = self.IsolatedProvider()

        register_provider(provider, registry=first)

        self.assertIs(get_provider(provider.name, registry=first), provider)
        self.assertIsNone(get_provider(provider.name, registry=second))
        self.assertIsNone(get_provider(provider.name))

    def test_fresh_registry_contains_builtin_aliases(self):
        registry = create_registry()
        self.assertIs(registry["openapi"], registry["openapi-raw"])
        self.assertIs(registry["json-file"], registry["json-file-raw"])

    def test_path_hash_validates_and_generates_a_format_neutral_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=root,
                check=True,
            )
            database = root / "database"
            database.mkdir()
            (database / "schema.sql").write_text(
                "CREATE TABLE widgets (id INTEGER PRIMARY KEY);\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "database/schema.sql"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "fixture"], cwd=root, check=True
            )
            config = {
                "project": "provider-contract",
                "components": {
                    "database": {
                        "path": "database",
                        "boundary": {
                            "provider": "path-hash",
                            "paths": ["schema.sql"],
                        },
                    }
                },
                "slices": {},
            }

            self.assertEqual(validate_config(config, root, source="head"), [])
            lockfile = generate_lockfile(config, root, source="head", strict=True)
            component = lockfile["components"]["database"]
            self.assertEqual(component["boundary_provider"], "path-hash")
            self.assertEqual(component["boundary_provider_version"], "3")
            self.assertEqual(component["boundary_status"], "ok")
            self.assertIsNotNone(component["fingerprints"]["boundary"])

    def test_custom_loader_registers_only_in_supplied_registry(self):
        module_name = "_boundver_provider_contract_fixture"
        module = types.ModuleType(module_name)
        module.IsolatedProvider = self.IsolatedProvider
        sys.modules[module_name] = module
        registry = create_registry()
        try:
            errors = load_custom_providers(
                [{"module": module_name, "class": "IsolatedProvider"}],
                allow_custom=True,
                registry=registry,
            )
            self.assertEqual(errors, [])
            self.assertIsNotNone(
                get_provider(self.IsolatedProvider.name, registry=registry)
            )
            self.assertIsNone(get_provider(self.IsolatedProvider.name))
        finally:
            del sys.modules[module_name]

    def test_custom_loader_reports_non_object_entries(self):
        errors = load_custom_providers(
            ["not-an-object"], allow_custom=True, registry=create_registry()
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("must be an object", errors[0])


class TestProviderHooks(unittest.TestCase):
    class HookProvider:
        name = "custom.hooks"

        def validate_config(self, boundary_cfg, component_path, repo_root):
            return [f"{component_path}: {boundary_cfg['problem']}"]

        def explain_diff(self, old_metadata, new_metadata, ctx):
            return f"{old_metadata['count']} -> {new_metadata['count']}"

    def test_validation_helper_invokes_provider_hook(self):
        self.assertEqual(
            validate_provider_config(
                self.HookProvider(), {"problem": "bad option"}, "svc", ROOT
            ),
            ["svc: bad option"],
        )

    def test_validation_helper_converts_hook_exception_to_error(self):
        class BrokenProvider(self.HookProvider):
            def validate_config(self, boundary_cfg, component_path, repo_root):
                raise RuntimeError("boom")

        errors = validate_provider_config(BrokenProvider(), {}, "svc", ROOT)
        self.assertEqual(len(errors), 1)
        self.assertIn("boom", errors[0])

    def test_validation_helper_rejects_a_malformed_result(self):
        class BrokenProvider(self.HookProvider):
            def validate_config(self, boundary_cfg, component_path, repo_root):
                return "not a list"

        errors = validate_provider_config(BrokenProvider(), {}, "svc", ROOT)
        self.assertEqual(len(errors), 1)
        self.assertIn("must return a list", errors[0])

    def test_explanation_helper_passes_metadata(self):
        self.assertEqual(
            explain_provider_diff(
                self.HookProvider(), {"count": 1}, {"count": 2}, _context()
            ),
            "1 -> 2",
        )

    def test_explanation_helper_survives_hook_exception(self):
        class BrokenProvider(self.HookProvider):
            def explain_diff(self, old_metadata, new_metadata, ctx):
                raise RuntimeError("boom")

        explanation = explain_provider_diff(
            BrokenProvider(), None, None, _context()
        )
        self.assertIn("custom.hooks boundary changed", explanation)
        self.assertIn("boom", explanation)

    def test_missing_hooks_have_backward_compatible_fallbacks(self):
        class LegacyProvider:
            name = "custom.legacy"

        provider = LegacyProvider()
        self.assertEqual(validate_provider_config(provider, {}, "svc", ROOT), [])
        self.assertEqual(
            explain_provider_diff(provider, None, None, _context()),
            "custom.legacy boundary changed",
        )


if __name__ == "__main__":
    unittest.main()
