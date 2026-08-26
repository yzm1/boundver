"""Unit tests for src/boundver/providers.py.

These tests do NOT require a git repository — provider logic is tested
with injected in-memory read_file / list_files callbacks.
"""
import unittest
from pathlib import Path
from typing import List
from unittest.mock import patch

from boundver._utils import (
    GuardrailError,
    MAX_GLOB_PATH_BYTES,
    MAX_GLOB_SEGMENTS,
    _match_path_glob,
)
from boundver.providers import (
    BoundaryProvider,
    ImplicitProvider,
    JsonCanonicalProvider,
    JsonFileProvider,
    LeafProvider,
    OpenApiCanonicalProvider,
    OpenApiProvider,
    PathHashProvider,
    ProviderContext,
    PythonExportsProvider,
    ResolvedBoundary,
    TypeScriptExportsProvider,
    compute_boundary,
    get_provider,
    load_custom_providers,
    register_provider,
)

ROOT = Path("/repo")


def _make_ctx(
    component_path: str = "svc",
    boundary_cfg: dict = None,
    source: str = "working-tree",
    files: dict = None,  # repo_rel_path → bytes
) -> ProviderContext:
    """Build a ProviderContext backed by an in-memory file store."""
    _files = files or {}

    def read_file(repo_rel: str) -> bytes:
        if repo_rel not in _files:
            raise FileNotFoundError(repo_rel)
        return _files[repo_rel]

    def list_files(prefix: str) -> List[str]:
        prefix = prefix.rstrip("/")
        return sorted(p for p in _files if p == prefix or p.startswith(prefix + "/"))

    return ProviderContext(
        repo_root=ROOT,
        component_path=component_path,
        boundary_cfg=boundary_cfg or {},
        source=source,
        read_file=read_file,
        list_files=list_files,
    )


class TestResolvedBoundary(unittest.TestCase):
    def test_defaults(self):
        rb = ResolvedBoundary()
        self.assertEqual(rb.entries, [])
        self.assertEqual(rb.status, "ok")
        self.assertEqual(rb.errors, [])
        self.assertIsNone(rb.metadata)

    def test_with_entries(self):
        rb = ResolvedBoundary(entries=[("file:api.yaml", b"data")], status="ok")
        self.assertEqual(len(rb.entries), 1)


class TestProviderContext(unittest.TestCase):
    def test_read_file_callback(self):
        ctx = _make_ctx(files={"svc/api.yaml": b"hello"})
        self.assertEqual(ctx.read_file("svc/api.yaml"), b"hello")

    def test_list_files_callback_exact_match(self):
        ctx = _make_ctx(files={"svc/api.yaml": b"x", "svc/sub/a.py": b"y", "other/b.py": b"z"})
        self.assertIn("svc/api.yaml", ctx.list_files("svc"))
        self.assertIn("svc/sub/a.py", ctx.list_files("svc"))
        self.assertNotIn("other/b.py", ctx.list_files("svc"))


class TestPathHashProvider(unittest.TestCase):
    def setUp(self):
        self.p = PathHashProvider()
        self.p.name = "test-path"

    def test_no_paths_returns_error(self):
        ctx = _make_ctx(boundary_cfg={})
        rb = self.p.resolve(ctx)
        self.assertEqual(rb.status, "error")
        self.assertFalse(rb.entries)

    def test_empty_paths_list_returns_error(self):
        ctx = _make_ctx(boundary_cfg={"paths": []})
        rb = self.p.resolve(ctx)
        self.assertEqual(rb.status, "error")

    def test_explicit_builtins_reject_missing_and_empty_paths_during_validation(self):
        explicit_providers = (
            PathHashProvider(),
            OpenApiProvider(),
            JsonFileProvider(),
            PythonExportsProvider(),
            TypeScriptExportsProvider(),
            JsonCanonicalProvider(),
            OpenApiCanonicalProvider(),
        )
        expected = ["No boundary paths declared for explicit boundary provider"]

        for provider in explicit_providers:
            for boundary_cfg in ({}, {"paths": []}):
                with self.subTest(
                    provider=provider.name,
                    boundary_cfg=boundary_cfg,
                ):
                    self.assertEqual(
                        provider.validate_config(boundary_cfg, "svc", ROOT),
                        expected,
                    )

    def test_paths_with_no_matching_files_returns_error(self):
        ctx = _make_ctx(boundary_cfg={"paths": ["missing.yaml"]}, files={})
        rb = self.p.resolve(ctx)
        self.assertEqual(rb.status, "error")
        self.assertTrue(
            any("matched no tracked files" in error for error in rb.errors),
            rb.errors,
        )

    def test_single_file_produces_ok_entry(self):
        ctx = _make_ctx(
            boundary_cfg={"paths": ["api.yaml"]},
            files={"svc/api.yaml": b"openapi: 3.0"},
        )
        rb = self.p.resolve(ctx)
        self.assertEqual(rb.status, "ok")
        self.assertEqual(len(rb.entries), 1)
        label, content = rb.entries[0]
        self.assertEqual(label, "file:api.yaml")
        self.assertEqual(content, b"openapi: 3.0")

    def test_format_neutral_provider_accepts_sql(self):
        ctx = _make_ctx(
            boundary_cfg={"provider": "path-hash", "paths": ["schema.sql"]},
            files={"svc/schema.sql": b"CREATE TABLE account (id bigint);\n"},
        )

        rb = PathHashProvider().resolve(ctx)

        self.assertEqual(rb.status, "ok")
        self.assertEqual(
            rb.entries,
            [("file:schema.sql", b"CREATE TABLE account (id bigint);\n")],
        )

    def test_multiple_files_sorted_by_path(self):
        ctx = _make_ctx(
            boundary_cfg={"paths": ["b.yaml", "a.yaml"]},
            files={"svc/a.yaml": b"A", "svc/b.yaml": b"B"},
        )
        rb = self.p.resolve(ctx)
        self.assertEqual(rb.status, "ok")
        labels = [label for label, _ in rb.entries]
        self.assertEqual(labels, sorted(labels))

    def test_crlf_normalised_to_lf(self):
        ctx = _make_ctx(
            boundary_cfg={"paths": ["file.txt"]},
            files={"svc/file.txt": b"line1\r\nline2\r\n"},
        )
        rb = self.p.resolve(ctx)
        _, content = rb.entries[0]
        self.assertNotIn(b"\r\n", content)
        self.assertIn(b"\n", content)

    def test_binary_content_not_crlf_normalised(self):
        # Binary files (containing \x00) must not have CRLF stripped.
        ctx = _make_ctx(
            boundary_cfg={"paths": ["img.png"]},
            files={"svc/img.png": b"\x89PNG\r\n\x1a\x0a\x00"},
        )
        rb = self.p.resolve(ctx)
        _, content = rb.entries[0]
        self.assertIn(b"\r\n", content)

    def test_validate_config_missing_file_returns_error(self):
        # validate_config checks filesystem existence — use a real tmp dir
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            errs = self.p.validate_config(
                {"paths": ["missing.txt"]},
                component_path="svc",
                repo_root=Path(td),
            )
            self.assertTrue(any("missing.txt" in e for e in errs))

    def test_validate_config_existing_file_is_ok(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_bytes(b"data")
            errs = self.p.validate_config(
                {"paths": ["api.yaml"]},
                component_path="svc",
                repo_root=root,
            )
            self.assertEqual(errs, [])

    def test_explain_diff_returns_string(self):
        ctx = _make_ctx()
        msg = self.p.explain_diff(None, None, ctx)
        self.assertIsInstance(msg, str)
        self.assertTrue(msg)

    def test_duplicate_paths_deduplicated(self):
        """Two entries in 'paths' that expand to the same file are deduplicated."""
        # Both "." and "api.yaml" expand to the same repo_rel path "svc/api.yaml"
        ctx = _make_ctx(
            boundary_cfg={"paths": ["api.yaml", "api.yaml"]},
            files={"svc/api.yaml": b"data"},
        )
        rb = self.p.resolve(ctx)
        self.assertEqual(rb.status, "ok")
        # Despite two path entries, only one entry should appear
        self.assertEqual(len(rb.entries), 1)


class TestComputeBoundary(unittest.TestCase):
    def test_digest_stability(self):
        """compute_boundary must use the shared framed boundary digest."""
        from boundver._hashing import HASH_DOMAIN_BOUNDARY, _hash_framed_entries

        content = b"openapi: 3.0\n"
        expected_digest = _hash_framed_entries(
            [("file:api.yaml", content)], domain=HASH_DOMAIN_BOUNDARY
        )

        p = PathHashProvider()
        p.name = "test"
        ctx = _make_ctx(
            boundary_cfg={"paths": ["api.yaml"]},
            files={"svc/api.yaml": content},
        )
        digest, status, errors = compute_boundary(p, ctx)
        self.assertEqual(digest, expected_digest)
        self.assertEqual(status, "ok")
        self.assertEqual(errors, [])

    def test_multi_file_digest_matches_framed_contract(self):
        """Multi-file digest must match the framed boundary format."""
        from boundver._hashing import HASH_DOMAIN_BOUNDARY, _hash_framed_entries

        files = {"svc/a.yaml": b"A\n", "svc/b.yaml": b"B\n"}
        expected = _hash_framed_entries(
            [("file:a.yaml", b"A\n"), ("file:b.yaml", b"B\n")],
            domain=HASH_DOMAIN_BOUNDARY,
        )

        p = PathHashProvider()
        p.name = "test"
        ctx = _make_ctx(
            boundary_cfg={"paths": ["a.yaml", "b.yaml"]},
            files=files,
        )
        digest, _, _ = compute_boundary(p, ctx)
        self.assertEqual(digest, expected)

    def test_error_provider_returns_none(self):
        p = PathHashProvider()
        p.name = "test"
        ctx = _make_ctx(boundary_cfg={})  # no paths → error
        digest, status, errors = compute_boundary(p, ctx)
        self.assertIsNone(digest)
        self.assertEqual(status, "error")

    def test_partial_status_propagated(self):
        p = ImplicitProvider()
        ctx = _make_ctx(boundary_cfg={})  # no paths → partial
        digest, status, errors = compute_boundary(p, ctx)
        self.assertIsNone(digest)
        self.assertEqual(status, "partial")

    def test_leaf_produces_no_digest_with_ok(self):
        p = LeafProvider()
        ctx = _make_ctx()
        digest, status, errors = compute_boundary(p, ctx)
        self.assertIsNone(digest)
        self.assertEqual(status, "ok")


class TestImplicitProvider(unittest.TestCase):
    def test_no_paths_is_partial(self):
        p = ImplicitProvider()
        ctx = _make_ctx(boundary_cfg={})
        rb = p.resolve(ctx)
        self.assertEqual(rb.status, "partial")
        self.assertFalse(rb.entries)

    def test_with_paths_delegates_to_path_hash(self):
        p = ImplicitProvider()
        ctx = _make_ctx(
            boundary_cfg={"paths": ["api.yaml"]},
            files={"svc/api.yaml": b"data"},
        )
        rb = p.resolve(ctx)
        self.assertEqual(rb.status, "ok")
        self.assertTrue(rb.entries)

    def test_validate_config_empty_is_ok(self):
        for boundary_cfg in ({}, {"paths": []}):
            with self.subTest(boundary_cfg=boundary_cfg):
                errs = ImplicitProvider().validate_config(
                    boundary_cfg,
                    "svc",
                    ROOT,
                )
                self.assertEqual(errs, [])

    def test_name(self):
        self.assertEqual(ImplicitProvider().name, "implicit")


class TestLeafProvider(unittest.TestCase):
    def test_always_ok_no_entries(self):
        p = LeafProvider()
        ctx = _make_ctx()
        rb = p.resolve(ctx)
        self.assertEqual(rb.status, "ok")
        self.assertEqual(rb.entries, [])

    def test_validate_config_always_ok(self):
        for boundary_cfg in ({}, {"paths": []}, {"paths": ["x"]}):
            with self.subTest(boundary_cfg=boundary_cfg):
                errs = LeafProvider().validate_config(
                    boundary_cfg,
                    "svc",
                    ROOT,
                )
                self.assertEqual(errs, [])

    def test_name(self):
        self.assertEqual(LeafProvider().name, "leaf")


class TestNamedProviders(unittest.TestCase):
    def _check_provider(self, cls, name: str):
        p = cls()
        self.assertEqual(p.name, name)
        # Resolves file paths correctly
        ctx = _make_ctx(
            boundary_cfg={"paths": ["schema.json"]},
            files={"svc/schema.json": b'{"x":1}'},
        )
        rb = p.resolve(ctx)
        self.assertEqual(rb.status, "ok")
        self.assertEqual(rb.entries[0][0], "file:schema.json")

    def test_openapi_provider(self):
        self._check_provider(OpenApiProvider, "openapi")

    def test_json_file_provider(self):
        self._check_provider(JsonFileProvider, "json-file")

    def test_python_exports_provider(self):
        self._check_provider(PythonExportsProvider, "python-exports")

    def test_typescript_exports_provider(self):
        self._check_provider(TypeScriptExportsProvider, "typescript-exports")


class TestRegistry(unittest.TestCase):
    def setUp(self):
        from boundver import providers as _p
        self._registry_snapshot = dict(_p._REGISTRY)

    def tearDown(self):
        from boundver import providers as _p
        _p._REGISTRY.clear()
        _p._REGISTRY.update(self._registry_snapshot)

    def test_builtin_providers_registered(self):
        for name in (
            "path-hash",
            "implicit",
            "leaf",
            "openapi",
            "json-file",
            "python-exports",
            "typescript-exports",
        ):
            self.assertIsNotNone(get_provider(name), f"Provider {name!r} not registered")

    def test_path_hash_registration_preserves_public_identity(self):
        provider = get_provider("path-hash")

        self.assertIsInstance(provider, PathHashProvider)
        self.assertEqual(provider.name, "path-hash")
        self.assertEqual(provider.version, "3")

    def test_get_unknown_returns_none(self):
        self.assertIsNone(get_provider("no-such-provider"))

    def test_register_custom_provider(self):
        class MyProvider(PathHashProvider):
            name = "custom.my-provider"

        register_provider(MyProvider())
        self.assertIsNotNone(get_provider("custom.my-provider"))

    def test_protocol_isinstance(self):
        p = ImplicitProvider()
        self.assertIsInstance(p, BoundaryProvider)


class TestPathHashIntegration(unittest.TestCase):
    def test_sql_boundary_validates_and_generates(self):
        import tempfile

        from boundver._config import validate_config
        from boundver._lockfile import generate_lockfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            component = root / "database"
            component.mkdir()
            (component / "schema.sql").write_text(
                "CREATE TABLE account (id bigint);\n",
                encoding="utf-8",
            )
            config = {
                "project": "format-neutral-provider",
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

            self.assertEqual(
                validate_config(config, root, source="working-tree"),
                [],
            )
            lockfile = generate_lockfile(
                config,
                root,
                source="working-tree",
            )

        entry = lockfile["components"]["database"]
        self.assertEqual(entry["boundary_provider"], "path-hash")
        self.assertEqual(entry["boundary_provider_version"], "3")
        self.assertEqual(entry["boundary_status"], "ok")
        self.assertIsNotNone(entry["fingerprints"]["boundary"])

    def test_empty_explicit_paths_fail_head_validation_before_default_generation(self):
        import subprocess
        import tempfile

        from boundver._config import validate_config
        from boundver._lockfile import generate_lockfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            component = root / "database"
            component.mkdir()
            (component / "schema.sql").write_text(
                "CREATE TABLE account (id bigint);\n",
                encoding="utf-8",
            )
            config = {
                "project": "explicit-provider-paths",
                "components": {
                    "database": {
                        "path": "database",
                        "boundary": {
                            "provider": "path-hash",
                            "paths": [],
                        },
                    }
                },
                "slices": {},
            }
            subprocess.run(
                ["git", "init", "-q"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "boundver@example.invalid"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Boundver Tests"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "add", "."],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-q", "-m", "fixture"],
                cwd=root,
                check=True,
                capture_output=True,
            )

            expected_errors = [
                "Component 'database': No boundary paths declared for "
                "explicit boundary provider"
            ]
            head_errors = validate_config(config, root, source="head")
            working_tree_errors = validate_config(
                config,
                root,
                source="working-tree",
            )

            self.assertEqual(head_errors, expected_errors)
            self.assertEqual(working_tree_errors, expected_errors)
            with self.assertRaisesRegex(
                ValueError,
                "No boundary paths declared for explicit boundary provider",
            ):
                generate_lockfile(config, root)

    def test_implicit_and_leaf_empty_paths_remain_valid(self):
        import tempfile

        from boundver._config import validate_config
        from boundver._lockfile import generate_lockfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for provider_name in ("implicit", "leaf"):
                component = root / provider_name
                component.mkdir()
                (component / "main.txt").write_text("content\n", encoding="utf-8")
                config = {
                    "project": f"{provider_name}-provider",
                    "components": {
                        provider_name: {
                            "path": provider_name,
                            "boundary": {
                                "provider": provider_name,
                                "paths": [],
                            },
                        }
                    },
                    "slices": {},
                }

                with self.subTest(provider=provider_name):
                    self.assertEqual(validate_config(config, root), [])
                    entry = generate_lockfile(
                        config,
                        root,
                        source="working-tree",
                    )["components"][provider_name]
                    self.assertIn(entry["boundary_status"], {"ok", "partial"})


class TestLoadCustomProviders(unittest.TestCase):
    def setUp(self):
        from boundver import providers as _p
        self._registry_snapshot = dict(_p._REGISTRY)

    def tearDown(self):
        from boundver import providers as _p
        _p._REGISTRY.clear()
        _p._REGISTRY.update(self._registry_snapshot)
    def test_empty_list_returns_no_errors(self):
        errs = load_custom_providers([], allow_custom=False)
        self.assertEqual(errs, [])

    def test_empty_list_with_allow_returns_no_errors(self):
        errs = load_custom_providers([], allow_custom=True)
        self.assertEqual(errs, [])

    def test_non_empty_without_allow_returns_error(self):
        errs = load_custom_providers(
            [{"module": "some.module", "class": "SomeClass"}],
            allow_custom=False,
        )
        self.assertTrue(errs)
        self.assertIn("--allow-custom-providers", errs[0])

    def test_missing_module_field_returns_error(self):
        errs = load_custom_providers(
            [{"class": "SomeClass"}],
            allow_custom=True,
        )
        self.assertTrue(errs)
        self.assertIn("module", errs[0])

    def test_missing_class_field_returns_error(self):
        errs = load_custom_providers(
            [{"module": "some.module"}],
            allow_custom=True,
        )
        self.assertTrue(errs)
        self.assertIn("class", errs[0])

    def test_import_failure_returns_error(self):
        errs = load_custom_providers(
            [{"module": "boundver._nonexistent_module_xyz", "class": "Foo"}],
            allow_custom=True,
        )
        self.assertEqual(len(errs), 1)
        self.assertIn("boundver._nonexistent_module_xyz", errs[0])

    def test_missing_class_in_module_returns_error(self):
        errs = load_custom_providers(
            [{"module": "boundver.providers", "class": "NonExistentClass"}],
            allow_custom=True,
        )
        self.assertEqual(len(errs), 1)
        self.assertIn("NonExistentClass", errs[0])

    def test_provider_name_not_custom_namespace_returns_error(self):
        # Patch an existing class with a non-custom name onto a valid module
        import sys, types

        fake_mod = types.ModuleType("_test_fake_providers_abc")

        class BadNameProvider:
            name = "not-custom"

            def resolve(self, ctx):
                return None

            def validate_config(self, c, p, r):
                return []

            def explain_diff(self, o, n, ctx):
                return ""

        fake_mod.BadNameProvider = BadNameProvider
        sys.modules["_test_fake_providers_abc"] = fake_mod
        try:
            errs = load_custom_providers(
                [{"module": "_test_fake_providers_abc", "class": "BadNameProvider"}],
                allow_custom=True,
            )
            self.assertEqual(len(errs), 1)
            self.assertIn("custom.", errs[0])
        finally:
            del sys.modules["_test_fake_providers_abc"]

    def test_valid_custom_provider_is_registered(self):
        import sys, types

        fake_mod = types.ModuleType("_test_valid_custom_provider_abc")

        class ValidProvider:
            name = "custom.test.valid-provider-abc"
            version = "1"

            def resolve(self, ctx):
                return ResolvedBoundary()

            def validate_config(self, c, p, r):
                return []

            def explain_diff(self, o, n, ctx):
                return ""

        fake_mod.ValidProvider = ValidProvider
        sys.modules["_test_valid_custom_provider_abc"] = fake_mod
        try:
            errs = load_custom_providers(
                [{"module": "_test_valid_custom_provider_abc", "class": "ValidProvider"}],
                allow_custom=True,
            )
            self.assertEqual(errs, [])
            self.assertIsNotNone(get_provider("custom.test.valid-provider-abc"))
        finally:
            del sys.modules["_test_valid_custom_provider_abc"]


class TestJsonCanonicalProvider(unittest.TestCase):
    def test_name(self):
        self.assertEqual(JsonCanonicalProvider().name, "json-canonical")

    def test_registered(self):
        from boundver.providers import get_provider
        self.assertIsNotNone(get_provider("json-canonical"))

    def test_no_paths_returns_error(self):
        p = JsonCanonicalProvider()
        ctx = _make_ctx(boundary_cfg={})
        rb = p.resolve(ctx)
        self.assertEqual(rb.status, "error")

    def test_valid_json_produces_canonical_entry(self):
        p = JsonCanonicalProvider()
        ctx = _make_ctx(
            boundary_cfg={"paths": ["schema.json"]},
            files={"svc/schema.json": b'{"b":2,"a":1}'},
        )
        rb = p.resolve(ctx)
        self.assertEqual(rb.status, "ok")
        self.assertEqual(len(rb.entries), 1)
        label, content = rb.entries[0]
        self.assertEqual(label, "canonical:schema.json")
        # Canonical: sorted keys, compact
        self.assertEqual(content, b'{"a":1,"b":2}')

    def test_key_ordering_does_not_affect_digest(self):
        """Two JSON files with same content in different key order produce the same digest."""
        p = JsonCanonicalProvider()
        ctx1 = _make_ctx(
            boundary_cfg={"paths": ["s.json"]},
            files={"svc/s.json": b'{"x":1,"y":2}'},
        )
        ctx2 = _make_ctx(
            boundary_cfg={"paths": ["s.json"]},
            files={"svc/s.json": b'{"y":2,"x":1}'},
        )
        from boundver.providers import compute_boundary
        d1, _, _ = compute_boundary(p, ctx1)
        d2, _, _ = compute_boundary(p, ctx2)
        self.assertEqual(d1, d2)

    def test_whitespace_does_not_affect_digest(self):
        from boundver.providers import compute_boundary
        p = JsonCanonicalProvider()
        ctx1 = _make_ctx(boundary_cfg={"paths": ["s.json"]},
                         files={"svc/s.json": b'{"a":1}'})
        ctx2 = _make_ctx(boundary_cfg={"paths": ["s.json"]},
                         files={"svc/s.json": b'{\n  "a": 1\n}'})
        d1, _, _ = compute_boundary(p, ctx1)
        d2, _, _ = compute_boundary(p, ctx2)
        self.assertEqual(d1, d2)

    def test_content_change_changes_digest(self):
        from boundver.providers import compute_boundary
        p = JsonCanonicalProvider()
        ctx1 = _make_ctx(boundary_cfg={"paths": ["s.json"]},
                         files={"svc/s.json": b'{"a":1}'})
        ctx2 = _make_ctx(boundary_cfg={"paths": ["s.json"]},
                         files={"svc/s.json": b'{"a":2}'})
        d1, _, _ = compute_boundary(p, ctx1)
        d2, _, _ = compute_boundary(p, ctx2)
        self.assertNotEqual(d1, d2)

    def test_invalid_json_returns_error(self):
        p = JsonCanonicalProvider()
        ctx = _make_ctx(boundary_cfg={"paths": ["bad.json"]},
                        files={"svc/bad.json": b"not json { }"})
        rb = p.resolve(ctx)
        self.assertEqual(rb.status, "error")
        self.assertTrue(any("JSON parse failed" in e for e in rb.errors))

    def test_explain_diff_returns_string(self):
        ctx = _make_ctx()
        msg = JsonCanonicalProvider().explain_diff(None, None, ctx)
        self.assertIsInstance(msg, str)

    def test_validate_config_missing_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            errs = JsonCanonicalProvider().validate_config(
                {"paths": ["missing.json"]}, "svc", Path(td))
            self.assertTrue(any("missing.json" in e for e in errs))

    def test_no_matching_files_returns_error(self):
        """Non-empty paths that produce no files via list_files hits the not-entries error path."""
        p = JsonCanonicalProvider()
        ctx = _make_ctx(
            boundary_cfg={"paths": ["nonexistent.json"]},
            files={},  # list_files returns nothing
        )
        rb = p.resolve(ctx)
        self.assertEqual(rb.status, "error")
        self.assertTrue(
            any("matched no tracked files" in error for error in rb.errors),
            rb.errors,
        )

    def test_digest_differs_from_raw_provider(self):
        """json-canonical digest must not collide with raw json-file digest for same file."""
        from boundver.providers import compute_boundary
        raw = b'{"a":1}'
        ctx_canonical = _make_ctx(boundary_cfg={"paths": ["s.json"]},
                                   files={"svc/s.json": raw})
        ctx_raw = _make_ctx(boundary_cfg={"paths": ["s.json"]},
                            files={"svc/s.json": raw})
        d_canonical, _, _ = compute_boundary(JsonCanonicalProvider(), ctx_canonical)
        d_raw, _, _ = compute_boundary(JsonFileProvider(), ctx_raw)
        self.assertNotEqual(d_canonical, d_raw)

    def test_raw_source_budget_is_independent_of_canonical_output(self):
        raw = b" " * 48 + b"{}"
        files = {
            "svc/a.json": raw,
            "svc/b.json": raw,
            "svc/c.json": raw,
        }
        requested_limits = []
        ctx = _make_ctx(
            boundary_cfg={"paths": ["*.json"]},
            files=files,
        )

        def read_limited(path, limit):
            requested_limits.append(limit)
            return files[path]

        ctx.read_file_limited = read_limited
        with patch("boundver.providers.MAX_PROVIDER_TOTAL_BYTES", 100):
            result = JsonCanonicalProvider().resolve(ctx)

        self.assertEqual(result.status, "error")
        self.assertEqual(result.entries, [])
        self.assertEqual(requested_limits, [100, 50, 0])
        self.assertIn("0-byte remaining limit", result.errors[0])


class TestOpenApiCanonicalProvider(unittest.TestCase):
    _SAMPLE_OPENAPI = b"""
openapi: "3.0.0"
info:
  title: My API
  version: "1.0.0"
  description: This description should not affect the digest.
servers:
  - url: https://api.example.com
tags:
  - name: users
    description: User operations
paths:
  /users:
    get:
      summary: List users
      description: Returns a list of users.
      operationId: listUsers
      x-internal: true
      parameters:
        - name: limit
          in: query
          description: Max results
          required: false
          schema:
            type: integer
      responses:
        "200":
          description: Success
          content:
            application/json:
              schema:
                type: array
                items:
                  $ref: "#/components/schemas/User"
components:
  schemas:
    User:
      description: A user object.
      type: object
      properties:
        id:
          type: string
          description: User ID
        name:
          type: string
"""

    def test_name(self):
        self.assertEqual(OpenApiCanonicalProvider().name, "openapi-canonical")

    def test_registered(self):
        from boundver.providers import get_provider
        self.assertIsNotNone(get_provider("openapi-canonical"))

    def test_no_paths_returns_error(self):
        rb = OpenApiCanonicalProvider().resolve(_make_ctx(boundary_cfg={}))
        self.assertEqual(rb.status, "error")

    def test_yaml_parses_and_strips_docs(self):
        p = OpenApiCanonicalProvider()
        ctx = _make_ctx(
            boundary_cfg={"paths": ["openapi.yaml"]},
            files={"svc/openapi.yaml": self._SAMPLE_OPENAPI},
        )
        rb = p.resolve(ctx)
        self.assertEqual(rb.status, "ok")
        label, content = rb.entries[0]
        self.assertEqual(label, "canonical:openapi.yaml")
        import json
        obj = json.loads(content.decode())
        # info, servers, tags stripped
        self.assertNotIn("info", obj)
        self.assertNotIn("servers", obj)
        self.assertNotIn("tags", obj)
        # paths kept
        self.assertIn("paths", obj)
        # descriptions stripped from paths
        get_op = obj["paths"]["/users"]["get"]
        self.assertNotIn("description", get_op)
        self.assertNotIn("summary", get_op)
        # x-* extensions may be contract-bearing and are retained.
        self.assertTrue(get_op["x-internal"])
        # operationId kept
        self.assertIn("operationId", get_op)
        # components kept
        self.assertIn("components", obj)
        # schema description stripped
        user = obj["components"]["schemas"]["User"]
        self.assertNotIn("description", user)
        # type kept
        self.assertIn("type", user)

    def test_description_change_does_not_change_digest(self):
        from boundver.providers import compute_boundary
        p = OpenApiCanonicalProvider()
        v1 = self._SAMPLE_OPENAPI
        v2 = self._SAMPLE_OPENAPI.replace(b"Returns a list of users.", b"Lists all users.")
        ctx1 = _make_ctx(boundary_cfg={"paths": ["api.yaml"]},
                         files={"svc/api.yaml": v1})
        ctx2 = _make_ctx(boundary_cfg={"paths": ["api.yaml"]},
                         files={"svc/api.yaml": v2})
        d1, _, _ = compute_boundary(p, ctx1)
        d2, _, _ = compute_boundary(p, ctx2)
        self.assertEqual(d1, d2)

    def test_endpoint_addition_changes_digest(self):
        from boundver.providers import compute_boundary
        p = OpenApiCanonicalProvider()

        base = {"openapi": "3.0.0", "paths": {"/users": {"get": {"operationId": "listUsers", "responses": {"200": {"content": {"application/json": {"schema": {"type": "array"}}}}}}}} }
        extended = dict(base)
        extended["paths"] = dict(base["paths"])
        extended["paths"]["/posts"] = {"get": {"operationId": "listPosts", "responses": {"200": {}}}}

        ctx1 = _make_ctx(boundary_cfg={"paths": ["api.yaml"]},
                         files={"svc/api.yaml": _json_to_yaml_bytes(base)})
        ctx2 = _make_ctx(boundary_cfg={"paths": ["api.yaml"]},
                         files={"svc/api.yaml": _json_to_yaml_bytes(extended)})
        d1, _, _ = compute_boundary(p, ctx1)
        d2, _, _ = compute_boundary(p, ctx2)
        self.assertNotEqual(d1, d2)

    def test_json_openapi_file_also_works(self):
        import json
        p = OpenApiCanonicalProvider()
        obj = {"openapi": "3.0.0", "paths": {"/ping": {"get": {"operationId": "ping", "responses": {"200": {}}}}}}
        raw = json.dumps(obj).encode()
        ctx = _make_ctx(boundary_cfg={"paths": ["api.json"]},
                        files={"svc/api.json": raw})
        rb = p.resolve(ctx)
        self.assertEqual(rb.status, "ok")
        self.assertEqual(rb.entries[0][0], "canonical:api.json")

    def test_whitespace_and_formatting_do_not_affect_digest(self):
        from boundver.providers import compute_boundary
        import json
        p = OpenApiCanonicalProvider()
        obj = {"openapi": "3.0.0", "paths": {"/ping": {"get": {"operationId": "ping", "responses": {"200": {}}}}}}
        compact = json.dumps(obj, separators=(",", ":")).encode()
        pretty = json.dumps(obj, indent=4).encode()
        ctx1 = _make_ctx(boundary_cfg={"paths": ["a.json"]}, files={"svc/a.json": compact})
        ctx2 = _make_ctx(boundary_cfg={"paths": ["a.json"]}, files={"svc/a.json": pretty})
        d1, _, _ = compute_boundary(p, ctx1)
        d2, _, _ = compute_boundary(p, ctx2)
        self.assertEqual(d1, d2)

    def test_invalid_yaml_returns_error(self):
        p = OpenApiCanonicalProvider()
        ctx = _make_ctx(boundary_cfg={"paths": ["bad.yaml"]},
                        files={"svc/bad.yaml": b": invalid: yaml: {"})
        rb = p.resolve(ctx)
        self.assertEqual(rb.status, "error")

    def test_explain_diff_returns_string(self):
        msg = OpenApiCanonicalProvider().explain_diff(None, None, _make_ctx())
        self.assertIsInstance(msg, str)

    def test_no_matching_files_returns_error(self):
        """Non-empty paths that produce no files hits the not-entries error path."""
        p = OpenApiCanonicalProvider()
        ctx = _make_ctx(
            boundary_cfg={"paths": ["nonexistent.yaml"]},
            files={},
        )
        rb = p.resolve(ctx)
        self.assertEqual(rb.status, "error")
        self.assertTrue(
            any("matched no tracked files" in error for error in rb.errors),
            rb.errors,
        )

    def test_validate_config_missing_file(self):
        """validate_config reports error for missing boundary path."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            errs = OpenApiCanonicalProvider().validate_config(
                {"paths": ["missing.yaml"]}, "svc", Path(td))
            self.assertTrue(any("missing.yaml" in e for e in errs))

    def test_digest_differs_from_raw_openapi_provider(self):
        """openapi-canonical digest must not collide with raw openapi digest for same file."""
        from boundver.providers import compute_boundary
        import json
        raw = json.dumps({"openapi": "3.0.0", "paths": {}}).encode()
        ctx1 = _make_ctx(boundary_cfg={"paths": ["api.json"]}, files={"svc/api.json": raw})
        ctx2 = _make_ctx(boundary_cfg={"paths": ["api.json"]}, files={"svc/api.json": raw})
        d_can, _, _ = compute_boundary(OpenApiCanonicalProvider(), ctx1)
        d_raw, _, _ = compute_boundary(OpenApiProvider(), ctx2)
        self.assertNotEqual(d_can, d_raw)

    def test_canonical_output_budget_is_independent_of_raw_source(self):
        raw = b"openapi: 3.1.0\npaths: {}"
        files = {
            "svc/a.yaml": raw,
            "svc/b.yaml": raw,
        }
        requested_limits = []
        ctx = _make_ctx(
            boundary_cfg={"paths": ["*.yaml"]},
            files=files,
        )

        def read_limited(path, limit):
            requested_limits.append(limit)
            return files[path]

        ctx.read_file_limited = read_limited
        raw_budget = len(raw) * len(files)
        with patch("boundver.providers.MAX_PROVIDER_TOTAL_BYTES", raw_budget):
            result = OpenApiCanonicalProvider().resolve(ctx)

        self.assertEqual(result.status, "error")
        self.assertEqual(result.entries, [])
        self.assertEqual(requested_limits, [raw_budget, len(raw)])
        self.assertIn("Canonical JSON", result.errors[0])


def _json_to_yaml_bytes(obj: dict) -> bytes:
    """Serialize a dict to YAML bytes for test fixtures."""
    try:
        import yaml  # type: ignore
        return yaml.dump(obj).encode()
    except ImportError:
        import json
        return json.dumps(obj).encode()


# ---------------------------------------------------------------------------
# Glob pattern tests (PathHashProvider / boundary.paths)
# ---------------------------------------------------------------------------

class TestGlobPatterns(unittest.TestCase):
    """boundary.paths entries containing *, ?, [ are glob patterns."""

    def test_star_matches_only_the_current_directory(self):
        """*.yaml is segment-local; recursive matches require **/*.yaml."""
        files = {
            "svc/root.yaml": b"openapi: 3.0.0",
            "svc/api/v1.yaml": b"openapi: 3.0.0",
            "svc/api/v2.yaml": b"openapi: 3.1.0",
            "svc/main.py":     b"# python",
        }
        ctx = _make_ctx(boundary_cfg={"paths": ["*.yaml"]}, files=files)
        result = PathHashProvider().resolve(ctx)
        self.assertEqual(result.status, "ok")
        labels = [label for label, _ in result.entries]
        self.assertEqual(labels, ["file:root.yaml"])
        self.assertNotIn("file:api/v1.yaml", labels)
        self.assertNotIn("file:api/v2.yaml", labels)
        self.assertNotIn("file:main.py", labels)

    def test_glob_with_directory_prefix(self):
        """api/*.yaml should match files under api/ only."""
        files = {
            "svc/api/openapi.yaml": b"openapi: 3.0.0",
            "svc/api/ignored.json": b"{}",
            "svc/other/schema.yaml": b"other: true",
        }
        ctx = _make_ctx(boundary_cfg={"paths": ["api/*.yaml"]}, files=files)
        result = PathHashProvider().resolve(ctx)
        labels = [label for label, _ in result.entries]
        self.assertIn("file:api/openapi.yaml", labels)
        # Ordinary * never crosses a path separator.
        self.assertNotIn("file:api/ignored.json", labels)

    def test_glob_and_literal_mixed(self):
        """Mixed literal + glob paths should both contribute entries."""
        files = {
            "svc/api/openapi.yaml": b"openapi: 3.0.0",
            "svc/schema.json":      b"{}",
            "svc/main.py":          b"pass",
        }
        ctx = _make_ctx(
            boundary_cfg={"paths": ["schema.json", "api/*.yaml"]},
            files=files,
        )
        result = PathHashProvider().resolve(ctx)
        labels = [label for label, _ in result.entries]
        self.assertIn("file:api/openapi.yaml", labels)
        self.assertIn("file:schema.json", labels)
        self.assertNotIn("file:main.py", labels)

    def test_glob_no_matches_returns_error(self):
        """A glob that matches nothing should yield an error result."""
        files = {"svc/main.py": b"pass"}
        ctx = _make_ctx(boundary_cfg={"paths": ["*.yaml"]}, files=files)
        result = PathHashProvider().resolve(ctx)
        self.assertEqual(result.status, "error")
        self.assertTrue(
            any("matched no tracked files" in error for error in result.errors),
            result.errors,
        )

    def test_matcher_exhaustion_discards_a_prior_successful_match(self):
        files = {
            "svc/target": b"short",
            "svc/z/z/z/z/z/target": b"long",
        }
        ctx = _make_ctx(
            boundary_cfg={"paths": ["**/target"]},
            files=files,
        )

        with patch("boundver._utils.MAX_GLOB_MATCH_STEPS", 30):
            self.assertTrue(_match_path_glob("target", "**/target"))
            with self.assertRaisesRegex(GuardrailError, "matcher steps"):
                _match_path_glob("z/z/z/z/z/target", "**/target")
            result = PathHashProvider().resolve(ctx)

        self.assertEqual(result.status, "error")
        self.assertEqual(result.entries, [])
        self.assertTrue(
            any("matcher steps" in error for error in result.errors),
            result.errors,
        )

    def test_candidate_size_limits_raise_controlled_guardrails(self):
        cases = (
            ("a" * (MAX_GLOB_PATH_BYTES + 1), "UTF-8 bytes"),
            ("/".join(["a"] * (MAX_GLOB_SEGMENTS + 1)), "segments"),
        )
        for candidate, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(GuardrailError, expected):
                    _match_path_glob(candidate, "**")

    def test_config_validation_reports_matcher_exhaustion(self):
        from boundver._config import validate_config

        files = ["svc/target", "svc/z/z/z/z/z/target"]
        config = {
            "project": "p",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {
                        "provider": "openapi",
                        "paths": ["**/target"],
                    },
                    "behavior": {"paths": ["**/target"]},
                }
            },
            "slices": {},
        }
        with (
            patch("boundver._config._list_files_for_source", return_value=files),
            patch("boundver._utils.MAX_GLOB_MATCH_STEPS", 12),
        ):
            errors = validate_config(config, ROOT, source="head")

        self.assertTrue(
            any(
                "path expansion could not be validated" in error
                and "matcher steps" in error
                for error in errors
            ),
            errors,
        )

    def test_glob_digest_changes_when_matched_file_changes(self):
        """Digest must change when a glob-matched file's content changes."""
        ctx1 = _make_ctx(
            boundary_cfg={"paths": ["*.yaml"]},
            files={"svc/api.yaml": b"v1"},
        )
        ctx2 = _make_ctx(
            boundary_cfg={"paths": ["*.yaml"]},
            files={"svc/api.yaml": b"v2"},
        )
        d1, _, _ = compute_boundary(PathHashProvider(), ctx1)
        d2, _, _ = compute_boundary(PathHashProvider(), ctx2)
        self.assertNotEqual(d1, d2)

    def test_glob_digest_changes_when_new_file_added(self):
        """Adding a file that matches the glob must change the digest."""
        ctx1 = _make_ctx(
            boundary_cfg={"paths": ["*.proto"]},
            files={"svc/service.proto": b"syntax = 'proto3';"},
        )
        ctx2 = _make_ctx(
            boundary_cfg={"paths": ["*.proto"]},
            files={
                "svc/service.proto": b"syntax = 'proto3';",
                "svc/events.proto":  b"syntax = 'proto3';",
            },
        )
        d1, _, _ = compute_boundary(PathHashProvider(), ctx1)
        d2, _, _ = compute_boundary(PathHashProvider(), ctx2)
        self.assertNotEqual(d1, d2)

    def test_validate_config_allows_glob_without_existence_check(self):
        """validate_config should not reject glob patterns for missing files."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            # No yaml files exist yet — glob should pass validation.
            errs = PathHashProvider().validate_config(
                {"paths": ["*.yaml"]}, "svc", root
            )
            self.assertEqual(errs, [])

    def test_validate_config_rejects_glob_with_traversal(self):
        """Glob patterns with '..' must be rejected."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            errs = PathHashProvider().validate_config(
                {"paths": ["../*.yaml"]}, "svc", Path(td)
            )
            self.assertTrue(any(".." in e for e in errs))

    def test_config_validate_allows_glob_paths(self):
        """_config.validate_config should not flag glob patterns as missing."""
        import tempfile
        from boundver.core import validate_config
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["*.yaml"]}}
                },
                "slices": {},
            }
            errs = validate_config(cfg, root)
            self.assertFalse(any("not found" in e for e in errs))

    def test_config_validate_rejects_glob_with_traversal(self):
        """_config.validate_config should reject glob patterns with '..'."""
        import tempfile
        from boundver.core import validate_config
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["../*.yaml"]}}
                },
                "slices": {},
            }
            errs = validate_config(cfg, root)
            self.assertTrue(any(".." in e for e in errs))


if __name__ == "__main__":
    unittest.main()
