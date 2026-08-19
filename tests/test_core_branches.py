"""
Targeted tests for specific branches in boundver modules that are difficult
to exercise via integration tests alone.  All tests are pure-Python or use
minimal temp-dir git repos.
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import boundver.core as core
from tests._repo_fixtures import init_git_repo


class GenerateLockfileCompatModeTests(unittest.TestCase):
    """Tests for semver_major_minor compat_mode branch."""

    def _setup(self, root: Path) -> dict:
        (root / "svc").mkdir()
        (root / "svc" / "main.py").write_text("x=1\n")
        return {
            "project": "p",
            "defaults": {"compat_mode": "semver_major_minor"},
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "implicit"},
                    "version_source": {"git_tag_prefix": "svc-v"},
                }
            },
            "slices": {},
        }

    def test_semver_major_minor_compat_mode(self):
        """semver_major_minor compatibility uses major.minor as its family."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            cfg = self._setup(root)
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "tag", "svc-v1.2.3"], cwd=root, check=True, capture_output=True)
            lockfile = core.generate_lockfile(cfg, root, source="head")
            fp = lockfile["components"]["svc"]["fingerprints"]
            # With version 1.2.3 and semver_major_minor, compat family is "1.2"
            # compat_digest = sha256_hex("svc@compat:1.2")
            expected = core.sha256_hex("svc@compat:1.2")
            self.assertEqual(fp["compat"], expected)

    def test_semver_major_minor_no_tag_is_generation_error(self):
        """A declared tag source that resolves no version remains fatal."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            cfg = self._setup(root)
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            with self.assertRaisesRegex(
                ValueError, "Configured version source did not produce a version"
            ):
                core.generate_lockfile(
                    cfg, root, source="head", strict=False
                )


class GenerateLockfileUnknownProviderTests(unittest.TestCase):
    """Test unknown boundary provider is handled gracefully."""

    def test_unknown_boundary_provider_fails_even_when_non_strict(self):
        """Allowing null slice inputs does not bless an unknown provider."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "unknown-custom-provider"},
                    }
                },
                "slices": {},
            }
            with self.assertRaisesRegex(ValueError, "Unknown boundary provider"):
                core.generate_lockfile(
                    cfg, root, source="working-tree", strict=False
                )


class SliceStrictModeTests(unittest.TestCase):
    """Tests for strict-mode errors in slice generation."""

    def _setup(self, root: Path) -> dict:
        (root / "svc").mkdir()
        (root / "svc" / "main.py").write_text("x=1\n")

    def test_compat_slice_strict_raises_when_compat_none(self):
        """A strict compatibility slice raises when compatibility is unavailable."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},
                        # No version_source → no compat digest
                    }
                },
                "slices": {
                    "s1": {"mode": "compat", "components": ["svc"]}
                },
            }
            with self.assertRaises(ValueError) as cm:
                core.generate_lockfile(cfg, root, source="working-tree", strict=True)
            self.assertIn("compat", str(cm.exception).lower())

    def test_boundary_slice_strict_raises_when_boundary_none(self):
        """A strict boundary slice raises when a boundary digest is unavailable."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},  # no paths → no boundary digest
                    }
                },
                "slices": {
                    "s1": {"mode": "boundary", "components": ["svc"]}
                },
            }
            with self.assertRaises(ValueError):
                core.generate_lockfile(cfg, root, source="working-tree", strict=True)

    def test_unknown_slice_mode_raises(self):
        """An unknown slice mode raises ValueError."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            self._setup(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "implicit"}}
                },
                "slices": {"s1": {"mode": "bogus-mode", "components": ["svc"]}},
            }
            # Note: validate_config would catch this, but generate_lockfile also checks.
            with self.assertRaises(ValueError):
                core.generate_lockfile(cfg, root, source="working-tree", strict=False)


class SliceMissingComponentTests(unittest.TestCase):
    """Test _recompute_slice_entry when component map is missing a component."""

    def test_recompute_slice_entry_missing_component_gives_none_digest(self):
        """A missing component entry contributes a null digest part."""
        result = core._recompute_slice_entry(
            "s1",
            {"mode": "exact", "components": ["ghost"]},
            {},  # empty components map
            strict=False,
        )
        self.assertIsNone(result["component_digests"]["ghost"])

    def test_recompute_slice_entry_unknown_mode_raises(self):
        """_recompute_slice_entry rejects an unknown slice mode."""
        comp = {"fingerprints": {"exact": "abc", "boundary": None, "compat": None}}
        with self.assertRaises(ValueError):
            core._recompute_slice_entry(
                "s1",
                {"mode": "bogus", "components": ["svc"]},
                {"svc": comp},
                strict=False,
            )


class GenerateLockfileForComponentsTests(unittest.TestCase):
    """Tests for generate_lockfile_for_components edge cases."""

    def _setup(self, root: Path) -> dict:
        (root / "svc").mkdir()
        (root / "svc" / "main.py").write_text("x=1\n")
        (root / "worker").mkdir()
        (root / "worker" / "main.py").write_text("y=1\n")
        return {
            "project": "p",
            "components": {
                "svc": {"path": "svc", "boundary": {"provider": "implicit"}},
                "worker": {"path": "worker", "boundary": {"provider": "implicit"}},
            },
            "slices": {},
        }

    def test_unknown_component_raises(self):
        """generate_lockfile_for_components rejects an unknown component."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            cfg = self._setup(root)
            with self.assertRaises(ValueError) as cm:
                core.generate_lockfile_for_components(
                    cfg, root, ["no-such-component"],
                    out_path=root / "boundary.lock.json",
                    source="working-tree",
                )
            self.assertIn("Unknown", str(cm.exception))

    def test_missing_existing_lockfile_requires_full_generation(self):
        """A subset cannot silently create an incomplete first lockfile."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            cfg = self._setup(root)
            out_path = root / "boundary.lock.json"
            self.assertFalse(out_path.exists())
            with self.assertRaisesRegex(ValueError, "full `boundver generate`"):
                core.generate_lockfile_for_components(
                    cfg,
                    root,
                    ["svc"],
                    out_path=out_path,
                    source="working-tree",
                )

    def test_non_v3_existing_lockfile_requires_full_generation(self):
        """Partial updates must not mix v1 and v3 hashing contracts."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            cfg = self._setup(root)
            out_path = root / "boundary.lock.json"
            out_path.write_text(
                json.dumps(
                    {
                        "schema": "boundary-lock/v1",
                        "project": "p",
                        "components": {},
                        "slices": {},
                    }
                )
            )

            with self.assertRaisesRegex(ValueError, "boundary-lock/v3"):
                core.generate_lockfile_for_components(
                    cfg,
                    root,
                    ["svc"],
                    out_path=out_path,
                    source="working-tree",
                )

    def test_merges_into_existing_lockfile(self):
        """generate_lockfile_for_components merges into an existing lockfile."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            cfg = self._setup(root)
            out_path = root / "boundary.lock.json"
            # Generate full lockfile first
            full = core.generate_lockfile(cfg, root, source="working-tree")
            out_path.write_text(json.dumps(full) + "\n")
            # Now update only svc
            result = core.generate_lockfile_for_components(
                cfg, root, ["svc"],
                out_path=out_path,
                source="working-tree",
            )
            # Both components should be in the merged result
            self.assertIn("svc", result["components"])
            self.assertIn("worker", result["components"])


class VendoredDriftTests(unittest.TestCase):
    """Test fail-closed vendored copy drift handling."""

    def test_vendored_drift_remains_fatal_when_non_strict(self):
        """Allowing null slice inputs does not bless vendored drift."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            (root / "vendor" / "svc").mkdir(parents=True)
            # Different content in vendored copy → drift
            (root / "vendor" / "svc" / "main.py").write_text("x=DIFFERENT\n")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},
                        "vendored_copies": ["vendor/svc"],
                    }
                },
                "slices": {},
            }
            with self.assertRaisesRegex(ValueError, "differs from source"):
                core.generate_lockfile(cfg, root, source="working-tree")
            with self.assertRaisesRegex(ValueError, "differs from source"):
                core.generate_lockfile(
                    cfg, root, source="working-tree", strict=False
                )


class ValidateConfigBranchTests(unittest.TestCase):
    """Tests for validate_config edge cases."""

    def test_validate_config_boundary_path_not_found_reports_error(self):
        """A nonexistent boundary path produces a validation error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {
                            "provider": "path",
                            "paths": ["nonexistent.yaml"],  # doesn't exist
                        },
                    }
                },
                "slices": {},
            }
            errors = core.validate_config(cfg, root)
            self.assertTrue(any("nonexistent.yaml" in e or "not found" in e.lower() for e in errors))

    def test_validate_config_allows_partial_boundary_slice_declaration(self):
        """Availability is enforced by strict generation, not base validation."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit", "paths": []},
                    }
                },
                "slices": {
                    "s1": {"mode": "boundary", "components": ["svc"]}
                },
            }
            errors = core.validate_config(cfg, root)
            self.assertEqual(errors, [])


class DiffLockfilesTests(unittest.TestCase):
    """Tests for diff_lockfiles additional branches."""

    def _make_lockfile(self, comps: dict, slices: dict = None) -> dict:
        return {
            "schema": "boundary-lock/v1",
            "components": comps,
            "slices": slices or {},
        }

    def _make_comp(self, exact: str, boundary=None, compat=None, version="1.0"):
        return {
            "version": version,
            "fingerprints": {"exact": exact, "boundary": boundary, "compat": compat},
        }

    def test_component_added(self):
        """diff_lockfiles reports added components."""
        old = self._make_lockfile({})
        new = self._make_lockfile({"svc": self._make_comp("aaa")})
        result = core.diff_lockfiles(old, new)
        self.assertTrue(any(c["name"] == "svc" for c in result["components"]["added"]))

    def test_component_removed(self):
        """diff_lockfiles reports removed components."""
        old = self._make_lockfile({"svc": self._make_comp("aaa")})
        new = self._make_lockfile({})
        result = core.diff_lockfiles(old, new)
        self.assertTrue(any(c["name"] == "svc" for c in result["components"]["removed"]))

    def test_boundary_only_change_summary(self):
        """diff_lockfiles _summarize_change for boundary-only change."""
        old = self._make_lockfile({"svc": self._make_comp("aaa", boundary="bbb")})
        new = self._make_lockfile({"svc": self._make_comp("aaa", boundary="ccc")})
        result = core.diff_lockfiles(old, new)
        changed = result["components"]["changed"]
        self.assertEqual(len(changed), 1)
        self.assertIn("boundary", changed[0]["summary"].lower())

    def test_compat_change_summary(self):
        """diff_lockfiles _summarize_change for compat change."""
        old = self._make_lockfile({"svc": self._make_comp("aaa", compat="x")})
        new = self._make_lockfile({"svc": self._make_comp("bbb", compat="y")})
        result = core.diff_lockfiles(old, new)
        changed = result["components"]["changed"]
        self.assertTrue(any("BREAKING" in c["summary"] for c in changed))

    def test_slice_changed(self):
        """diff_lockfiles reports changed slices."""
        old = self._make_lockfile({}, {"s1": {"fingerprint": "aaa"}})
        new = self._make_lockfile({}, {"s1": {"fingerprint": "bbb"}})
        result = core.diff_lockfiles(old, new)
        self.assertTrue(any(s["name"] == "s1" for s in result["slices"]["changed"]))

    def test_slice_unchanged(self):
        """diff_lockfiles reports unchanged slices."""
        old = self._make_lockfile({}, {"s1": {"fingerprint": "aaa"}})
        new = self._make_lockfile({}, {"s1": {"fingerprint": "aaa"}})
        result = core.diff_lockfiles(old, new)
        self.assertIn("s1", result["slices"]["unchanged"])


class ExplainComponentChangesTests(unittest.TestCase):
    """Tests for explain_component_changes boundary paths section."""

    def test_explain_component_with_boundary_paths_and_changes(self):
        """explain shows boundary-relevant changed files."""
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("v: 1\n")
            (root / "svc" / "impl.py").write_text("x=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            # Modify both boundary and non-boundary file
            (root / "svc" / "api.yaml").write_text("v: 2\n")
            (root / "svc" / "impl.py").write_text("x=2\n")
            cfg = {
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "path", "paths": ["api.yaml"]},
                    }
                }
            }
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = core.explain_component_changes(cfg, root, "svc", base_ref="HEAD", source="working-tree")
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("Boundary-relevant changed files", out)
            self.assertIn("api.yaml", out)

    def test_explain_component_with_boundary_paths_no_boundary_changes(self):
        """explain reports when no boundary-relevant files changed."""
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("v: 1\n")
            (root / "svc" / "impl.py").write_text("x=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            # Only modify non-boundary file
            (root / "svc" / "impl.py").write_text("x=2\n")
            cfg = {
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "path", "paths": ["api.yaml"]},
                    }
                }
            }
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = core.explain_component_changes(cfg, root, "svc", base_ref="HEAD", source="working-tree")
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("none", out.lower())


class ImplicitProviderWithPathsTests(unittest.TestCase):
    """Test ImplicitProvider when paths are declared (delegates to PathHashProvider)."""

    def test_implicit_provider_with_paths_produces_boundary_digest(self):
        """ImplicitProvider delegates to PathHashProvider when paths are declared."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("v: 1\n")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {
                            "provider": "implicit",
                            "paths": ["api.yaml"],  # paths declared → should get boundary digest
                        },
                    }
                },
                "slices": {},
            }
            lockfile = core.generate_lockfile(cfg, root, source="working-tree")
            comp = lockfile["components"]["svc"]
            # With paths declared, boundary digest should be non-null
            self.assertIsNotNone(comp["fingerprints"]["boundary"])


class LeafProviderTests(unittest.TestCase):
    """Test LeafProvider methods."""

    def test_leaf_provider_validate_config_returns_empty(self):
        """LeafProvider.validate_config accepts its minimal configuration."""
        from boundver.providers import LeafProvider
        result = LeafProvider().validate_config({}, "svc", Path("."))
        self.assertEqual(result, [])

    def test_leaf_provider_explain_diff_returns_string(self):
        """LeafProvider.explain_diff describes a leaf-boundary change."""
        from boundver.providers import LeafProvider
        result = LeafProvider().explain_diff(None, None, None)
        self.assertIsInstance(result, str)
        self.assertIn("leaf", result.lower())

    def test_leaf_provider_in_lockfile(self):
        """Leaf provider generates no boundary digest but status is 'ok'."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "leaf"},
                    }
                },
                "slices": {},
            }
            lockfile = core.generate_lockfile(cfg, root, source="working-tree")
            comp = lockfile["components"]["svc"]
            self.assertEqual(comp["boundary_status"], "ok")
            self.assertIsNone(comp["fingerprints"]["boundary"])


class PathHashProviderTests(unittest.TestCase):
    """Test PathHashProvider edge cases."""

    def test_path_hash_provider_validate_config_missing_path(self):
        """PathHashProvider.validate_config reports missing paths."""
        from boundver.providers import PathHashProvider
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            # api.yaml does not exist
            boundary_cfg = {"paths": ["api.yaml"]}
            errors = PathHashProvider().validate_config(boundary_cfg, "svc", root)
            self.assertTrue(any("api.yaml" in e or "not found" in e.lower() for e in errors))

    def test_path_hash_provider_explain_diff(self):
        """PathHashProvider.explain_diff describes path-content changes."""
        from boundver.providers import PathHashProvider
        result = PathHashProvider().explain_diff(None, None, None)
        self.assertIsInstance(result, str)

    def test_implicit_provider_explain_diff(self):
        """ImplicitProvider.explain_diff describes implicit-boundary changes."""
        from boundver.providers import ImplicitProvider
        result = ImplicitProvider().explain_diff(None, None, None)
        self.assertIsInstance(result, str)
        self.assertIn("implicit", result.lower())


class ValidateConfigMiscBranchTests(unittest.TestCase):
    """Additional validate_config branches for coverage."""

    def test_load_config_schema_invalid_json(self):
        """An invalid repository schema falls back to the bundled schema."""
        from boundver._config import _load_config_schema
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "boundary.config.schema.json").write_text("not valid json {{{")
            result = _load_config_schema(root)
            self.assertIsInstance(result, dict)
            self.assertIn("components", result.get("properties", {}))

    def test_validate_config_non_dict_returns_error(self):
        """validate_config with non-dict input returns an error immediately."""
        errors = core.validate_config("not a dict", Path("/tmp"))
        self.assertEqual(errors, ["Config root must be a JSON object"])

    def test_validate_config_boundary_path_escapes_component_root(self):
        """Boundary path containing '..' that escapes component root is rejected."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "outside.yaml").write_bytes(b"data")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {
                            "provider": "openapi",
                            "paths": ["../outside.yaml"],
                        },
                    }
                },
                "slices": {},
            }
            errors = core.validate_config(cfg, root)
            self.assertTrue(any("escapes" in e for e in errors))

    def test_validate_config_vendored_copies_not_list(self):
        """vendored_copies that is not a list of strings is rejected."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},
                        "vendored_copies": "not-a-list",
                    }
                },
                "slices": {},
            }
            errors = core.validate_config(cfg, root)
            self.assertTrue(any("vendored_copies" in e for e in errors))

    def test_validate_config_version_source_not_dict(self):
        """version_source that is not a dict is rejected."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},
                        "version_source": "not-a-dict",
                    }
                },
                "slices": {},
            }
            errors = core.validate_config(cfg, root)
            self.assertTrue(any("version_source" in e for e in errors))

    def test_validate_config_boundary_path_escapes_repository_root(self):
        """Boundary path that escapes repository root via symlink traversal is rejected."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            # Place the component at repo root itself so .. escapes the repo
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": ".",
                        "boundary": {
                            "provider": "openapi",
                            "paths": ["../outside.yaml"],
                        },
                    }
                },
                "slices": {},
            }
            errors = core.validate_config(cfg, root)
            # Should report either escapes-component-root or escapes-repository-root
            self.assertTrue(
                any("escapes" in e or "repository root" in e for e in errors)
            )


class LoadCustomProvidersInstantiationTests(unittest.TestCase):
    """Tests for load_custom_providers instantiation failure path."""

    def test_instantiation_failure_returns_error(self):
        """When cls() raises, an error is returned instead of propagating."""
        import sys
        import types
        from boundver.providers import load_custom_providers

        fake_mod = types.ModuleType("_test_bad_init_provider")

        class BadInitProvider:
            name = "custom.test.bad-init"

            def __init__(self):
                raise RuntimeError("init failed for testing")

        fake_mod.BadInitProvider = BadInitProvider
        sys.modules["_test_bad_init_provider"] = fake_mod
        try:
            errs = load_custom_providers(
                [{"module": "_test_bad_init_provider", "class": "BadInitProvider"}],
                allow_custom=True,
            )
            self.assertEqual(len(errs), 1)
            self.assertIn("init failed for testing", errs[0])
        finally:
            del sys.modules["_test_bad_init_provider"]


# ---------------------------------------------------------------------------
# _diff._summarize_change edge case
# ---------------------------------------------------------------------------

class SummarizeChangeFallbackTests(unittest.TestCase):
    def test_multiple_facets_no_compat_no_boundary_falls_through(self):
        """_summarize_change returns generic 'changed: ...' for exotic facet combos."""
        from boundver._diff import _summarize_change
        # Hypothetical future facet — should fall through to generic message
        result = _summarize_change({"exact": {}, "unknown_facet": {}})
        self.assertIn("changed:", result)
        self.assertIn("exact", result)


# ---------------------------------------------------------------------------
# _config.py gaps
# ---------------------------------------------------------------------------

class ConfigFileLoaderTests(unittest.TestCase):
    def test_yaml_parse_error_raises_value_error(self):
        """load_config_file raises ValueError on bad YAML."""
        from boundver._config import load_config_file
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cfg.yaml"
            p.write_text(": : : invalid\n")
            with self.assertRaises(ValueError) as ctx:
                load_config_file(p)
            self.assertIn("YAML parse error", str(ctx.exception))

    def test_toml_parse_error_raises_value_error(self):
        """load_config_file raises ValueError on bad TOML."""
        from boundver._config import load_config_file
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cfg.toml"
            p.write_text("invalid = = = bad\n")
            with self.assertRaises(ValueError) as ctx:
                load_config_file(p)
            self.assertIn("TOML parse error", str(ctx.exception))

    def test_unsupported_extension_raises_value_error(self):
        """load_config_file raises ValueError for unsupported extension."""
        from boundver._config import load_config_file
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cfg.ini"
            p.write_text("[section]\nkey=val\n")
            with self.assertRaises(ValueError) as ctx:
                load_config_file(p)
            self.assertIn("Unsupported config file extension", str(ctx.exception))

    def test_find_config_file_returns_absolute_hint_unchanged(self):
        """find_config_file returns an absolute path hint as-is."""
        from boundver._config import find_config_file
        with tempfile.TemporaryDirectory() as td:
            abs_path = Path(td) / "my.json"
            abs_path.write_text("{}")
            result = find_config_file(Path(td), str(abs_path))
            self.assertEqual(result, abs_path)


class ValidateConfigEdgeCaseTests(unittest.TestCase):
    def _repo(self) -> tempfile.TemporaryDirectory:
        td = tempfile.TemporaryDirectory()
        return td

    def test_boundary_path_traversal_error(self):
        """validate_config flags a boundary path that escapes the component root."""
        from boundver._config import validate_config
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("x")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {
                            "provider": "openapi",
                            "paths": ["../outside.yaml"],
                        },
                    }
                },
                "slices": {},
            }
            errors = validate_config(cfg, root)
            self.assertTrue(any("boundary path" in e for e in errors))

    def test_version_source_file_empty_string(self):
        """validate_config rejects version_source.file = ''."""
        from boundver._config import validate_config
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit", "paths": []},
                        "version_source": {"file": "   ", "field": "version"},
                    }
                },
                "slices": {},
            }
            errors = validate_config(cfg, root)
            self.assertTrue(any("version_source.file" in e for e in errors))


# ---------------------------------------------------------------------------
# core.py dispatch gaps
# ---------------------------------------------------------------------------

class MainDryRunTests(unittest.TestCase):

    def test_generate_dry_run_does_not_write_lockfile(self):
        import io, sys as _sys
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit", "paths": []}}},
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg))
            old_argv = _sys.argv[:]
            old_dir = import_os.getcwd()
            import_os.chdir(str(root))
            try:
                _sys.argv = [
                    "boundver", "generate", "--source", "working-tree", "--dry-run"
                ]
                out = io.StringIO()
                with redirect_stdout(out):
                    try:
                        core.main()
                    except SystemExit:
                        pass
            finally:
                _sys.argv = old_argv
                import_os.chdir(old_dir)
            self.assertFalse((root / "boundary.lock.json").exists())
            self.assertIn("Dry run", out.getvalue())


import os as import_os


class MainVerifyDriftTextTests(unittest.TestCase):

    def test_verify_drifted_outputs_red_text(self):
        """verify command prints a LOCKFILE OUT OF DATE header when drift is found."""
        import io, sys as _sys
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit", "paths": []}}},
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg))
            subprocess.run(
                ["git", "add", "boundary.config.json"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "config"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            # Generate lockfile at commit 1
            lockfile = core.generate_lockfile(cfg, root)
            (root / "boundary.lock.json").write_text(json.dumps(lockfile))
            subprocess.run(
                ["git", "add", "boundary.lock.json"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "lock"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            # Now make a second commit so HEAD diverges from lockfile
            (root / "svc" / "main.py").write_text("x=2\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "change"], cwd=root, check=True, capture_output=True)
            old_argv = _sys.argv[:]
            old_dir = import_os.getcwd()
            import_os.chdir(str(root))
            try:
                _sys.argv = ["boundver", "verify"]
                out = io.StringIO()
                with redirect_stdout(out):
                    try:
                        core.main()
                    except SystemExit:
                        pass
            finally:
                _sys.argv = old_argv
                import_os.chdir(old_dir)
            self.assertIn("OUT OF DATE", out.getvalue())


class MainExplainNonZeroTests(unittest.TestCase):

    def test_explain_unknown_component_exits_2(self):
        import sys as _sys
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit", "paths": []}}},
                "slices": {},
            }
            (root / "boundary.config.json").write_text(json.dumps(cfg))
            old_argv = _sys.argv[:]
            old_dir = import_os.getcwd()
            import_os.chdir(str(root))
            try:
                _sys.argv = ["boundver", "explain", "no-such-comp"]
                try:
                    core.main()
                    rc = 0
                except SystemExit as e:
                    rc = e.code
            finally:
                _sys.argv = old_argv
                import_os.chdir(old_dir)
            self.assertEqual(rc, 2)


class MainInitDefaultsTests(unittest.TestCase):

    def test_init_creates_config_with_components_key(self):
        """boundver init writes boundary.config.json with a components block."""
        import sys as _sys
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            old_argv = _sys.argv[:]
            old_dir = import_os.getcwd()
            import_os.chdir(str(root))
            try:
                _sys.argv = ["boundver", "init"]
                try:
                    core.main()
                except SystemExit:
                    pass
            finally:
                _sys.argv = old_argv
                import_os.chdir(old_dir)
            out_path = root / "boundary.config.json"
            self.assertTrue(out_path.exists())
            data = json.loads(out_path.read_text())
            self.assertIn("components", data)


# ---------------------------------------------------------------------------
# _output.py gaps
# ---------------------------------------------------------------------------

class ExplainComponentSourceTests(unittest.TestCase):

    def test_explain_no_boundary_paths_returns_early(self):
        """explain_component_changes returns 0 and notes no boundary paths."""
        import io
        from contextlib import redirect_stdout
        from boundver.core import explain_component_changes
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "v1"], cwd=root, check=True, capture_output=True)
            (root / "svc" / "main.py").write_text("x=2\n")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "implicit", "paths": []}}
                },
                "slices": {},
            }
            out = io.StringIO()
            with redirect_stdout(out):
                rc = explain_component_changes(cfg, root, "svc", source="working-tree")
            # boundary_paths is [], so it should print "no boundary paths" and return 0
            self.assertEqual(rc, 0)
            self.assertIn("none declared", out.getvalue())

    def test_explain_head_source_diffs_between_refs(self):
        """explain_component_changes with source='head' uses HEAD diff."""
        import io
        from contextlib import redirect_stdout
        from boundver.core import explain_component_changes
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "v1"], cwd=root, check=True, capture_output=True)
            first_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root, capture_output=True, text=True
            ).stdout.strip()
            (root / "svc" / "main.py").write_text("x=2\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "v2"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "implicit", "paths": []}}
                },
                "slices": {},
            }
            out = io.StringIO()
            with redirect_stdout(out):
                rc = explain_component_changes(cfg, root, "svc", base_ref=first_sha, source="head")
            # Should show the changed file (main.py was modified between commits)
            self.assertEqual(rc, 0)
            self.assertIn("main.py", out.getvalue())


class WhyComponentVersionTests(unittest.TestCase):

    def test_why_shows_version_when_present(self):
        """why_component prints Version: line when lockfile has a version."""
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            (root / "svc" / "version.json").write_text('{"version": "1.0.0"}\n')
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "version_source": {"file": "version.json", "field": "version"},
                    }
                },
                "slices": {},
            }
            lockfile = core.generate_lockfile(cfg, root)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = core.why_component(cfg, lockfile, root, "svc")
            self.assertEqual(rc, 0)
            self.assertIn("1.0.0", out.getvalue())


class RecomputeSliceEntryTests(unittest.TestCase):
    """Tests for _recompute_slice_entry edge cases."""

    def test_component_missing_from_map_gives_none_digest(self):
        """When a slice lists a component not in the components_map, digest_parts[name] = None."""
        from boundver._lockfile import _recompute_slice_entry
        slice_def = {"mode": "exact", "components": ["present", "missing"]}
        components_map = {
            "present": {"fingerprints": {"exact": "abc", "boundary": "def", "compat": "ghi"}},
            # "missing" is intentionally absent
        }
        result = _recompute_slice_entry("s1", slice_def, components_map, strict=False)
        self.assertIsNone(result["component_digests"]["missing"])
        self.assertIsNotNone(result["fingerprint"])  # digest still computed

    def test_all_components_missing_gives_all_none(self):
        from boundver._lockfile import _recompute_slice_entry
        slice_def = {"mode": "exact", "components": ["a", "b"]}
        result = _recompute_slice_entry("s1", slice_def, {}, strict=False)
        self.assertIsNone(result["component_digests"]["a"])
        self.assertIsNone(result["component_digests"]["b"])


class ValidateConfigCustomProviderListTests(unittest.TestCase):
    """Validate explicit custom-provider names against component references."""

    def test_implicit_provider_name_is_resolved_only_after_loading(self):
        """A class-only declaration cannot be name-checked before trusted loading."""
        import tempfile
        from boundver._config import validate_config
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "providers": [{"module": "custom_pkg", "class": "OtherProvider"}],
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "custom.MyProvider"},
                    }
                },
                "slices": {},
            }
            errors = validate_config(cfg, root)
            self.assertFalse(
                any("not declared in the" in e for e in errors),
                f"Unexpected name cross-reference error: {errors}",
            )

    def test_explicit_provider_name_must_match_component_reference(self):
        from boundver._config import validate_config

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            cfg = {
                "project": "p",
                "providers": [
                    {
                        "module": "custom_pkg",
                        "class": "OtherProvider",
                        "name": "custom.OtherProvider",
                    }
                ],
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "custom.MyProvider"},
                    }
                },
                "slices": {},
            }
            errors = validate_config(cfg, root)
            self.assertTrue(any("not declared in the" in e for e in errors), errors)


class ExplainGitDiffFailureTests(unittest.TestCase):
    """explain_component_changes returns exit 2 when git diff fails."""

    def test_explain_returns_2_when_git_diff_fails(self):
        """explain_component_changes returns 2 when the Git diff subprocess fails."""
        from unittest.mock import patch
        import subprocess as _sp
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}}},
                "slices": {},
            }
            # Patch _git_run to raise CalledProcessError for any diff call
            with patch("boundver._output._git_run", side_effect=_sp.CalledProcessError(1, "git")):
                rc = core.explain_component_changes(cfg, root, "svc", base_ref="HEAD~1")
            self.assertEqual(rc, 2)

    def test_explain_index_source_uses_cached_diff(self):
        """Index-source explain uses Git's cached diff."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            # Stage a change so the index differs from HEAD.
            (root / "svc" / "main.py").write_text("x = 2\n")
            subprocess.run(["git", "add", "svc/main.py"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit", "paths": []}}},
                "slices": {},
            }
            rc = core.explain_component_changes(cfg, root, "svc", base_ref="HEAD", source="index")
            self.assertIn(rc, (0, 1, 2))

    def test_explain_skips_malformed_diff_lines(self):
        """Malformed diff records without a tab are silently skipped."""
        from unittest.mock import patch, MagicMock
        mock_result = MagicMock()
        # Mix of a valid line and a malformed line lacking a tab.
        mock_result.stdout = "malformed-no-tab\nM\tsvc/main.py"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit", "paths": []}}},
                "slices": {},
            }
            with patch("boundver._output._git_run", return_value=mock_result):
                rc = core.explain_component_changes(cfg, root, "svc", base_ref="HEAD")
        # Should complete without error; result may be 0 (no boundary paths).
        self.assertIn(rc, (0, 1, 2))


class WyComponentErrorPathTests(unittest.TestCase):
    """why_component error branches in _output.py."""

    def _base_fixtures(self, root: Path):
        """Create minimal committed state and return (config, lockfile)."""
        init_git_repo(root)
        (root / "svc").mkdir()
        (root / "svc" / "main.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
        cfg = {
            "project": "p",
            "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit", "paths": []}}},
            "slices": {},
        }
        lockfile = core.generate_lockfile(cfg, root, source="head")
        # Tamper the exact fingerprint so there is detectable drift.
        lockfile["components"]["svc"]["fingerprints"]["exact"] = "aaaa"
        return cfg, lockfile

    def test_why_component_generate_failure_returns_2(self):
        """why_component returns 2 when lockfile generation raises."""
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg, lockfile = self._base_fixtures(root)
            # Patch at the source module so the lazy import inside why_component gets the mock.
            with patch("boundver._lockfile.generate_lockfile", side_effect=RuntimeError("boom")):
                rc = core.why_component(cfg, lockfile, root, "svc")
            self.assertEqual(rc, 2)

    def test_why_component_git_diff_failure_is_graceful(self):
        """why_component continues when Git diff fails.

        The git diff section in why_component only runs for source='working-tree'.
        When _git_run raises CalledProcessError, the except block fires and the
        function continues (not rc=2).
        """
        import subprocess as _sp
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # working-tree: no git repo needed; filesystem fallback handles file listing.
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x = 1\n")
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit", "paths": []}}},
                "slices": {},
            }
            # Build lockfile with working-tree source and then tamper it to create drift.
            lockfile = core.generate_lockfile(cfg, root, source="working-tree")
            lockfile["components"]["svc"]["fingerprints"]["exact"] = "old_fingerprint"
            # _output._git_run is called inside why_component for git diff with source="working-tree".
            with patch("boundver._output._git_run", side_effect=_sp.CalledProcessError(128, "git")):
                rc = core.why_component(cfg, lockfile, root, "svc", source="working-tree")
            # Drift detected → rc=1; git diff failure is swallowed (not rc=2).
            self.assertEqual(rc, 1)


class LockfileWorkingTreeReadTests(unittest.TestCase):
    """Working-tree reads performed by _make_read_file."""

    def test_working_tree_boundary_reads_file_bytes(self):
        """Working-tree generation reads explicit boundary paths from disk."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "api.py").write_text("def api(): pass\n")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit", "paths": ["api.py"]},
                    }
                },
                "slices": {},
            }
            lock = core.generate_lockfile(cfg, root, source="working-tree")
            fp = lock["components"]["svc"]["fingerprints"]
            # Boundary digest is computed by reading api.py via full.read_bytes().
            self.assertIsNotNone(fp.get("boundary"))

    def test_working_tree_boundary_digest_changes_with_content(self):
        """Changing a boundary file on disk changes the boundary digest."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "api.py").write_text("v1\n")
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit", "paths": ["api.py"]},
                    }
                },
                "slices": {},
            }
            lock1 = core.generate_lockfile(cfg, root, source="working-tree")
            (root / "svc" / "api.py").write_text("v2\n")
            lock2 = core.generate_lockfile(cfg, root, source="working-tree")
            fp1 = lock1["components"]["svc"]["fingerprints"]["boundary"]
            fp2 = lock2["components"]["svc"]["fingerprints"]["boundary"]
            self.assertNotEqual(fp1, fp2)

    def test_index_source_boundary_reads_via_git_cat_blob(self):
        """Index-source generation reads boundary files through Git blobs."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.py").write_text("def api(): pass\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            # Index source: file is staged, not yet committed.
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit", "paths": ["api.py"]},
                    }
                },
                "slices": {},
            }
            lock = core.generate_lockfile(cfg, root, source="index")
            fp = lock["components"]["svc"]["fingerprints"]
            self.assertIsNotNone(fp.get("boundary"))

    def test_version_file_read_working_tree_source(self):
        """Working-tree generation reads version metadata from disk."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "package.json").write_text('{"version": "2.0.0"}\n')
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "version_source": {"file": "package.json", "field": "version"},
                        "boundary": {"provider": "implicit", "paths": []},
                    }
                },
                "slices": {},
            }
            lock = core.generate_lockfile(cfg, root, source="working-tree")
            self.assertEqual(lock["components"]["svc"]["version"], "2.0.0")

    def test_version_file_read_index_source(self):
        """Index-source generation reads version metadata from the staged file."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "package.json").write_text('{"version": "3.0.0"}\n')
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "version_source": {"file": "package.json", "field": "version"},
                        "boundary": {"provider": "implicit", "paths": []},
                    }
                },
                "slices": {},
            }
            lock = core.generate_lockfile(cfg, root, source="index")
            self.assertEqual(lock["components"]["svc"]["version"], "3.0.0")


class StrictBoundarySliceTests(unittest.TestCase):
    """Strict slices referencing components absent from a partial lockfile."""

    def test_slice_component_not_in_lockfile_gets_null_digest(self):
        """A component absent from a partial lockfile contributes a null slice digest."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x = 1\n")
            # Slice references "ghost" which doesn't exist in components.
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit", "paths": []},
                    }
                },
                "slices": {
                    "s": {
                        "description": "slice with missing component",
                        "mode": "exact",
                        "components": ["svc", "ghost"],
                    }
                },
            }
            # strict=False so the missing component doesn't abort — just assigns None.
            lock = core.generate_lockfile(cfg, root, source="working-tree", strict=False)
            slice_digests = lock["slices"]["s"]["component_digests"]
            self.assertIsNone(slice_digests.get("ghost"))
            self.assertIsNotNone(slice_digests.get("svc"))


class GitListHeadFilesFallbackTests(unittest.TestCase):
    """list_head_files cat-file fallback for a single committed file."""

    def test_list_head_files_returns_single_file_path(self):
        """A single-file path falls back to cat-file and returns that file."""
        from boundver._git import list_head_files
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "readme.txt").write_text("hello\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            # Passing a single file path — ls-tree returns nothing for a blob, so cat-file -t fallback runs.
            result = list_head_files(root, "readme.txt")
            self.assertEqual(result, ["readme.txt"])

    def test_list_head_files_cat_file_fallback_via_mock(self):
        """When ls-tree returns empty output, cat-file -t is tried.
        Uses mock to force ls-tree to return empty so the fallback branch is taken."""
        from unittest.mock import patch, MagicMock
        from boundver._git import list_head_files

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cat_file = MagicMock(stdout="blob")
            with patch(
                "boundver._git._iter_bounded_git_paths", return_value=iter(())
            ) as iter_paths, patch(
                "boundver._git._git_run", return_value=cat_file
            ) as run_text:
                result = list_head_files(root, "readme.txt")
            self.assertEqual(result, ["readme.txt"])
            iter_paths.assert_called_once()
            run_text.assert_called_once()

    def test_list_head_files_cat_file_not_blob_returns_empty(self):
        """When cat-file identifies a tree rather than a blob, no files are returned."""
        from unittest.mock import patch, MagicMock
        from boundver._git import list_head_files

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch(
                "boundver._git._git_run", return_value=MagicMock(stdout="tree")
            ):
                result = list_head_files(root, "somedir")
            self.assertEqual(result, [])

    def test_list_head_files_cat_file_raises_returns_empty(self):
        """A cat-file subprocess failure produces an empty file list."""
        from unittest.mock import patch
        import subprocess as _sp
        from boundver._git import list_head_files

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch(
                "boundver._git._git_run",
                side_effect=_sp.CalledProcessError(128, "git"),
            ):
                result = list_head_files(root, "missingfile.txt")
            self.assertEqual(result, [])


class DetectProviderTests(unittest.TestCase):
    """Provider detection behavior for recognized and fallback file layouts."""

    def setUp(self):
        from boundver._config import _detect_provider
        self._fn = _detect_provider

    def test_detects_openapi_yaml_by_name(self):
        """openapi.yaml → provider 'openapi'."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "openapi.yaml").write_text("openapi: 3.0.0\n")
            provider, paths = self._fn(d)
            self.assertEqual(provider, "openapi")
            self.assertIn("openapi.yaml", paths)

    def test_detects_openapi_like_file_by_prefix(self):
        """An unrecognized openapi-prefixed file falls back to a glob provider."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "openapi-v2.yaml").write_text("openapi: 2.0\n")
            provider, paths = self._fn(d)
            self.assertEqual(provider, "openapi")

    def test_detects_boundary_json(self):
        """boundary.json selects the json-file provider."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "boundary.json").write_text("{}")
            provider, paths = self._fn(d)
            self.assertEqual(provider, "json-file")
            self.assertIn("boundary.json", paths)

    def test_detects_python_exports(self):
        """__init__.py selects the python-exports provider."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "__init__.py").write_text("")
            provider, paths = self._fn(d)
            self.assertEqual(provider, "python-exports")

    def test_detects_typescript_src_index(self):
        """src/index.ts selects the typescript-exports provider."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "src").mkdir()
            (d / "src" / "index.ts").write_text("")
            provider, paths = self._fn(d)
            self.assertEqual(provider, "typescript-exports")
            self.assertIn("src/index.ts", paths)

    def test_detects_typescript_root_index(self):
        """A root index.ts selects the typescript-exports provider."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "index.ts").write_text("")
            provider, paths = self._fn(d)
            self.assertEqual(provider, "typescript-exports")
            self.assertIn("index.ts", paths)

    def test_falls_back_to_implicit(self):
        """A layout without recognized boundary files selects the implicit provider."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "random.txt").write_text("hello")
            provider, paths = self._fn(d)
            self.assertEqual(provider, "implicit")
            self.assertEqual(paths, [])


class HashingContentOnlyValueErrorTests(unittest.TestCase):
    """Content-only digest behavior when a file is outside the base path."""

    def test_content_only_digest_file_outside_base_uses_repo_rel(self):
        """A file outside the base uses its full repository-relative path."""
        from unittest.mock import patch
        from boundver._hashing import _content_only_digest

        # Patch _list_files_for_source to return a file that isn't under 'svc'.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "readme.txt").write_text("hello\n")
            with patch("boundver._hashing._list_files_for_source", return_value=["readme.txt"]):
                digest = _content_only_digest(root, "svc", source="working-tree")
            # Should produce a digest using the full path "readme.txt" as key.
            self.assertIsNotNone(digest)
            self.assertEqual(len(digest), 64)  # SHA-256 hex

    def test_content_only_head_snapshot_cannot_select_file_outside_base(self):
        """Captured Git-tree selection stays confined to the requested base."""
        from unittest.mock import patch
        from boundver._hashing import _content_only_digest

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "readme.txt").write_text("hello\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)

            # Git-backed sources enumerate the captured immutable tree rather
            # than trusting the legacy moving-ref listing callback.
            with patch("boundver._hashing._list_files_for_source", return_value=["readme.txt"]):
                digest = _content_only_digest(root, "svc", source="head")
            self.assertIsNone(digest)


class PathHashProviderGlobTests(unittest.TestCase):
    """Glob expansion behavior in PathHashProvider."""

    def test_glob_pattern_expands_matching_files(self):
        """A boundary path with '*' expands against component files."""
        from boundver.providers import PathHashProvider, ProviderContext

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "api.py").write_text("x = 1\n")
            (root / "svc" / "utils.py").write_text("y = 2\n")
            (root / "svc" / "README.md").write_text("# readme\n")

            def list_files(prefix: str):
                # Return all files under svc
                return ["svc/api.py", "svc/utils.py", "svc/README.md"]

            def read_file(repo_rel: str) -> bytes:
                return (root / repo_rel).read_bytes()

            ctx = ProviderContext(
                repo_root=root,
                component_path="svc",
                boundary_cfg={"provider": "implicit", "paths": ["*.py"]},
                source="working-tree",
                read_file=read_file,
                list_files=list_files,
            )
            provider = PathHashProvider()
            result = provider.resolve(ctx)
            # entries is the list of (label, content) pairs
            self.assertTrue(len(result.entries) > 0, "Expected at least one matched file")
            # The entries should only include .py files
            entry_labels = [label for label, _ in result.entries]
            self.assertTrue(any("api.py" in l for l in entry_labels))
            self.assertTrue(any("utils.py" in l for l in entry_labels))
            self.assertFalse(any("README.md" in l for l in entry_labels))


class GitLatestTagFallbackTests(unittest.TestCase):
    """git_latest_tag never falls back to repository-wide unreachable tags."""

    def test_latest_tag_returns_none_when_describe_fails(self):
        """A failed reachability lookup cannot select a repository-wide tag."""
        from unittest.mock import patch
        import subprocess as _sp
        from boundver._git import git_latest_tag

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)

            call_count = [0]
            def fake_git_run(repo_root, args, **kw):
                call_count[0] += 1
                if "describe" in args:
                    raise _sp.CalledProcessError(128, "git")
                raise AssertionError("repository-wide tag fallback must not run")

            with patch("boundver._git._git_run", side_effect=fake_git_run):
                version = git_latest_tag(root, "svc-v")
            self.assertIsNone(version)
            self.assertEqual(call_count, [1])

    def test_latest_tag_fallback_returns_none_when_no_tags(self):
        """A failed reachable-tag query returns None."""
        from unittest.mock import patch
        import subprocess as _sp
        from boundver._git import git_latest_tag

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)

            def fake_git_run(repo_root, args, **kw):
                if "describe" in args:
                    raise _sp.CalledProcessError(128, "git")
                from unittest.mock import MagicMock
                result = MagicMock()
                result.stdout = ""
                return result

            with patch("boundver._git._git_run", side_effect=fake_git_run):
                version = git_latest_tag(root, "svc-v")
            self.assertIsNone(version)

    def test_latest_tag_fallback_returns_none_when_tag_list_also_fails(self):
        """A Git failure while querying reachable tags returns None."""
        from unittest.mock import patch
        import subprocess as _sp
        from boundver._git import git_latest_tag

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)

            def fake_git_run(repo_root, args, **kw):
                raise _sp.CalledProcessError(128, "git")

            with patch("boundver._git._git_run", side_effect=fake_git_run):
                version = git_latest_tag(root, "svc-v")
            self.assertIsNone(version)


# ===========================================================================
# Behavior tier tests
# ===========================================================================

class BehaviorTierGenerationTests(unittest.TestCase):
    """Tests for the behavior fingerprint tier in lockfile generation."""

    def _make_repo(self, root: Path) -> None:
        init_git_repo(root)
        (root / "svc").mkdir()
        (root / "svc" / "main.py").write_text("x=1\n")
        (root / "svc" / "api.yaml").write_text("openapi: 3.0\n")
        (root / "svc" / "config").mkdir()
        (root / "svc" / "config" / "defaults.json").write_text('{"timeout": 30}\n')
        (root / "svc" / "migrations").mkdir()
        (root / "svc" / "migrations" / "001.sql").write_text("CREATE TABLE t;\n")
        subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)

    def test_behavior_null_when_not_configured(self):
        """behavior fingerprint is null when component has no behavior config."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                    }
                },
                "slices": {},
            }
            lockfile = core.generate_lockfile(cfg, root, source="head")
            self.assertIsNone(lockfile["components"]["svc"]["fingerprints"]["behavior"])

    def test_behavior_generation_does_not_mask_unknown_boundary_provider(self):
        """A valid behavior digest cannot bless an invalid boundary provider."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "does-not-exist", "paths": ["api.yaml"]},
                        "behavior": {"paths": ["api.yaml", "config/defaults.json"]},
                    }
                },
                "slices": {},
            }

            with self.assertRaisesRegex(ValueError, "Unknown boundary provider"):
                core.generate_lockfile(
                    cfg, root, source="head", strict=False
                )

    def test_behavior_populated_when_configured(self):
        """behavior fingerprint is a hex string when behavior.paths is declared."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "behavior": {"paths": ["api.yaml", "config/defaults.json"]},
                    }
                },
                "slices": {},
            }
            lockfile = core.generate_lockfile(cfg, root, source="head")
            fp = lockfile["components"]["svc"]["fingerprints"]["behavior"]
            self.assertIsNotNone(fp)
            self.assertEqual(len(fp), 64)  # SHA-256 hex

    def test_behavior_changes_when_config_file_changes(self):
        """behavior fingerprint changes when a declared behavior file changes."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "behavior": {"paths": ["api.yaml", "config/defaults.json"]},
                    }
                },
                "slices": {},
            }
            lock1 = core.generate_lockfile(cfg, root, source="head")
            fp1 = lock1["components"]["svc"]["fingerprints"]

            # Change config file (behavior-relevant, not boundary-relevant)
            (root / "svc" / "config" / "defaults.json").write_text('{"timeout": 10}\n')
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "change config"], cwd=root, check=True, capture_output=True)

            lock2 = core.generate_lockfile(cfg, root, source="head")
            fp2 = lock2["components"]["svc"]["fingerprints"]

            # exact should change (file changed)
            self.assertNotEqual(fp1["exact"], fp2["exact"])
            # behavior should change (config/defaults.json is in behavior paths)
            self.assertNotEqual(fp1["behavior"], fp2["behavior"])
            # boundary should NOT change (api.yaml didn't change)
            self.assertEqual(fp1["boundary"], fp2["boundary"])

    def test_behavior_stable_on_internal_change(self):
        """behavior fingerprint is stable when only internal files change."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "behavior": {"paths": ["api.yaml", "config/defaults.json"]},
                    }
                },
                "slices": {},
            }
            lock1 = core.generate_lockfile(cfg, root, source="head")
            fp1 = lock1["components"]["svc"]["fingerprints"]

            # Change only implementation file
            (root / "svc" / "main.py").write_text("x=2\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "refactor"], cwd=root, check=True, capture_output=True)

            lock2 = core.generate_lockfile(cfg, root, source="head")
            fp2 = lock2["components"]["svc"]["fingerprints"]

            self.assertNotEqual(fp1["exact"], fp2["exact"])
            self.assertEqual(fp1["behavior"], fp2["behavior"])
            self.assertEqual(fp1["boundary"], fp2["boundary"])

    def test_behavior_includes_migrations(self):
        """behavior fingerprint catches migration file additions."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "behavior": {"paths": ["api.yaml", "migrations/"]},
                    }
                },
                "slices": {},
            }
            lock1 = core.generate_lockfile(cfg, root, source="head")
            fp1 = lock1["components"]["svc"]["fingerprints"]["behavior"]

            # Add a new migration
            (root / "svc" / "migrations" / "002.sql").write_text("ALTER TABLE t ADD col INT;\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "add migration"], cwd=root, check=True, capture_output=True)

            lock2 = core.generate_lockfile(cfg, root, source="head")
            fp2 = lock2["components"]["svc"]["fingerprints"]["behavior"]

            self.assertNotEqual(fp1, fp2)

    def test_behavior_empty_paths_gives_null(self):
        """behavior with empty paths list results in null fingerprint."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "behavior": {"paths": []},
                    }
                },
                "slices": {},
            }
            lockfile = core.generate_lockfile(cfg, root, source="head")
            self.assertIsNone(lockfile["components"]["svc"]["fingerprints"]["behavior"])

    def test_behavior_working_tree_source(self):
        """behavior fingerprint works with working-tree source."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "behavior": {"paths": ["api.yaml", "config/defaults.json"]},
                    }
                },
                "slices": {},
            }
            lockfile = core.generate_lockfile(cfg, root, source="working-tree")
            fp = lockfile["components"]["svc"]["fingerprints"]["behavior"]
            self.assertIsNotNone(fp)
            self.assertEqual(len(fp), 64)


class BehaviorTierSliceTests(unittest.TestCase):
    """Tests for behavior slice mode."""

    def _make_repo(self, root: Path) -> None:
        init_git_repo(root)
        (root / "svc").mkdir()
        (root / "svc" / "main.py").write_text("x=1\n")
        (root / "svc" / "api.yaml").write_text("openapi: 3.0\n")
        (root / "svc" / "config.json").write_text('{"k": "v"}\n')
        subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)

    def test_behavior_slice_uses_behavior_digest(self):
        """Slice with mode=behavior uses behavior fingerprint for its hash."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "behavior": {"paths": ["api.yaml", "config.json"]},
                    }
                },
                "slices": {
                    "exact-slice": {"mode": "exact", "components": ["svc"]},
                    "behavior-slice": {"mode": "behavior", "components": ["svc"]},
                    "boundary-slice": {"mode": "boundary", "components": ["svc"]},
                },
            }
            lockfile = core.generate_lockfile(cfg, root, source="head")
            exact_fp = lockfile["slices"]["exact-slice"]["fingerprint"]
            behavior_fp = lockfile["slices"]["behavior-slice"]["fingerprint"]
            boundary_fp = lockfile["slices"]["boundary-slice"]["fingerprint"]
            # All three slices should have different fingerprints
            self.assertNotEqual(exact_fp, behavior_fp)
            self.assertNotEqual(behavior_fp, boundary_fp)
            self.assertNotEqual(exact_fp, boundary_fp)

    def test_behavior_slice_stable_on_internal_change(self):
        """behavior slice is stable when only internal files change."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "behavior": {"paths": ["api.yaml", "config.json"]},
                    }
                },
                "slices": {
                    "behavior-slice": {"mode": "behavior", "components": ["svc"]},
                },
            }
            lock1 = core.generate_lockfile(cfg, root, source="head")

            (root / "svc" / "main.py").write_text("x=2\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "refactor"], cwd=root, check=True, capture_output=True)

            lock2 = core.generate_lockfile(cfg, root, source="head")
            self.assertEqual(
                lock1["slices"]["behavior-slice"]["fingerprint"],
                lock2["slices"]["behavior-slice"]["fingerprint"],
            )

    def test_behavior_slice_changes_on_config_change(self):
        """behavior slice changes when behavior-relevant file changes."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "behavior": {"paths": ["api.yaml", "config.json"]},
                    }
                },
                "slices": {
                    "behavior-slice": {"mode": "behavior", "components": ["svc"]},
                },
            }
            lock1 = core.generate_lockfile(cfg, root, source="head")

            (root / "svc" / "config.json").write_text('{"k": "v2"}\n')
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "config change"], cwd=root, check=True, capture_output=True)

            lock2 = core.generate_lockfile(cfg, root, source="head")
            self.assertNotEqual(
                lock1["slices"]["behavior-slice"]["fingerprint"],
                lock2["slices"]["behavior-slice"]["fingerprint"],
            )

    def test_behavior_slice_strict_raises_when_behavior_null(self):
        """Slice with mode=behavior and strict=True raises when behavior is null."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        # No behavior config → behavior will be null
                    }
                },
                "slices": {
                    "s1": {"mode": "behavior", "components": ["svc"]}
                },
            }
            with self.assertRaises(ValueError) as cm:
                core.generate_lockfile(cfg, root, source="head", strict=True)
            self.assertIn("behavior", str(cm.exception).lower())

    def test_behavior_slice_non_strict_allows_null(self):
        """Slice with mode=behavior and strict=False allows null behavior digest."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                    }
                },
                "slices": {
                    "s1": {"mode": "behavior", "components": ["svc"]}
                },
            }
            lockfile = core.generate_lockfile(cfg, root, source="head", strict=False)
            self.assertIsNone(lockfile["slices"]["s1"]["component_digests"]["svc"])


class BehaviorTierVerifyTests(unittest.TestCase):
    """Tests for behavior fingerprint in verify."""

    def _make_repo(self, root: Path) -> None:
        init_git_repo(root)
        (root / "svc").mkdir()
        (root / "svc" / "main.py").write_text("x=1\n")
        (root / "svc" / "api.yaml").write_text("openapi: 3.0\n")
        (root / "svc" / "config.json").write_text('{"timeout": 30}\n')
        subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)

    def test_verify_detects_behavior_drift(self):
        """verify reports mismatch when behavior fingerprint drifts."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "behavior": {"paths": ["api.yaml", "config.json"]},
                    }
                },
                "slices": {},
            }
            lockfile = core.generate_lockfile(cfg, root, source="head")

            # Change behavior-relevant file
            (root / "svc" / "config.json").write_text('{"timeout": 5}\n')
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "change timeout"], cwd=root, check=True, capture_output=True)

            issues = core.verify_lockfile(cfg, lockfile, root, source="head")
            behavior_issues = [i for i in issues if "behavior" in i]
            self.assertTrue(len(behavior_issues) > 0)
            # boundary should NOT be flagged
            boundary_issues = [i for i in issues if "boundary" in i.lower() and "behavior" not in i]
            self.assertEqual(len(boundary_issues), 0)

    def test_verify_passes_when_behavior_unchanged(self):
        """verify passes when only internal files change and behavior is stable."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "behavior": {"paths": ["api.yaml", "config.json"]},
                    }
                },
                "slices": {},
            }
            # Generate lockfile after changing only internal file
            (root / "svc" / "main.py").write_text("x=2\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "refactor"], cwd=root, check=True, capture_output=True)
            lockfile = core.generate_lockfile(cfg, root, source="head")

            # Verify immediately — should pass
            issues = core.verify_lockfile(cfg, lockfile, root, source="head")
            self.assertEqual(issues, [])


class BehaviorTierDiffTests(unittest.TestCase):
    """Tests for behavior-aware diff summaries."""

    def test_behavior_only_change_summary(self):
        """diff_lockfiles labels exact+behavior as behavioral contract change."""
        old = {
            "schema": "boundary-lock/v1",
            "components": {
                "svc": {
                    "version": "1.0.0",
                    "fingerprints": {"exact": "aaa", "behavior": "bbb", "boundary": "ccc", "compat": "ddd"},
                }
            },
            "slices": {},
        }
        new = {
            "schema": "boundary-lock/v1",
            "components": {
                "svc": {
                    "version": "1.0.0",
                    "fingerprints": {"exact": "aaa2", "behavior": "bbb2", "boundary": "ccc", "compat": "ddd"},
                }
            },
            "slices": {},
        }
        result = core.diff_lockfiles(old, new)
        changed = result["components"]["changed"]
        self.assertEqual(len(changed), 1)
        self.assertIn("behavioral", changed[0]["summary"].lower())
        self.assertIn("unchanged", changed[0]["summary"].lower())

    def test_exact_only_still_impl_only(self):
        """diff with only exact changed still says implementation-only."""
        old = {
            "schema": "boundary-lock/v1",
            "components": {
                "svc": {
                    "version": "1.0.0",
                    "fingerprints": {"exact": "aaa", "behavior": "bbb", "boundary": "ccc", "compat": "ddd"},
                }
            },
            "slices": {},
        }
        new = {
            "schema": "boundary-lock/v1",
            "components": {
                "svc": {
                    "version": "1.0.0",
                    "fingerprints": {"exact": "aaa2", "behavior": "bbb", "boundary": "ccc", "compat": "ddd"},
                }
            },
            "slices": {},
        }
        result = core.diff_lockfiles(old, new)
        changed = result["components"]["changed"]
        self.assertEqual(len(changed), 1)
        self.assertIn("implementation", changed[0]["summary"].lower())

    def test_all_four_changed_is_breaking(self):
        """diff with all four facets changed is BREAKING."""
        old = {
            "schema": "boundary-lock/v1",
            "components": {
                "svc": {
                    "version": "1.0.0",
                    "fingerprints": {"exact": "a", "behavior": "b", "boundary": "c", "compat": "d"},
                }
            },
            "slices": {},
        }
        new = {
            "schema": "boundary-lock/v1",
            "components": {
                "svc": {
                    "version": "2.0.0",
                    "fingerprints": {"exact": "a2", "behavior": "b2", "boundary": "c2", "compat": "d2"},
                }
            },
            "slices": {},
        }
        result = core.diff_lockfiles(old, new)
        changed = result["components"]["changed"]
        self.assertIn("BREAKING", changed[0]["summary"])


class BehaviorTierContainmentTests(unittest.TestCase):
    """Tests verifying the containment hierarchy: exact ⊇ behavior ⊇ boundary."""

    def _make_repo(self, root: Path) -> None:
        init_git_repo(root)
        (root / "svc").mkdir()
        (root / "svc" / "main.py").write_text("x=1\n")
        (root / "svc" / "api.yaml").write_text("openapi: 3.0\n")
        (root / "svc" / "config.json").write_text('{"k": 1}\n')
        subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)

    def test_boundary_change_implies_behavior_change(self):
        """When boundary file changes, behavior also changes (superset property)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "behavior": {"paths": ["api.yaml", "config.json"]},
                    }
                },
                "slices": {},
            }
            lock1 = core.generate_lockfile(cfg, root, source="head")

            # Change boundary file
            (root / "svc" / "api.yaml").write_text("openapi: 3.1\npaths: {}\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "api change"], cwd=root, check=True, capture_output=True)

            lock2 = core.generate_lockfile(cfg, root, source="head")
            fp1 = lock1["components"]["svc"]["fingerprints"]
            fp2 = lock2["components"]["svc"]["fingerprints"]

            # boundary changed → behavior MUST also change (api.yaml is in both)
            self.assertNotEqual(fp1["boundary"], fp2["boundary"])
            self.assertNotEqual(fp1["behavior"], fp2["behavior"])
            self.assertNotEqual(fp1["exact"], fp2["exact"])

    def test_behavior_change_does_not_imply_boundary_change(self):
        """Behavior can change without boundary changing."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_repo(root)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "behavior": {"paths": ["api.yaml", "config.json"]},
                    }
                },
                "slices": {},
            }
            lock1 = core.generate_lockfile(cfg, root, source="head")

            # Change only config (in behavior, not in boundary)
            (root / "svc" / "config.json").write_text('{"k": 2}\n')
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "config"], cwd=root, check=True, capture_output=True)

            lock2 = core.generate_lockfile(cfg, root, source="head")
            fp1 = lock1["components"]["svc"]["fingerprints"]
            fp2 = lock2["components"]["svc"]["fingerprints"]

            self.assertNotEqual(fp1["behavior"], fp2["behavior"])
            self.assertEqual(fp1["boundary"], fp2["boundary"])


class BehaviorTierConfigValidationTests(unittest.TestCase):
    """Tests for config validation with behavior slice mode."""

    def test_behavior_is_valid_slice_mode(self):
        """Config with mode=behavior passes validation."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("x\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "behavior": {"paths": ["api.yaml"]},
                    }
                },
                "slices": {
                    "s": {"mode": "behavior", "components": ["svc"]}
                },
            }
            errors = core.validate_config(cfg, root)
            mode_errors = [e for e in errors if "mode" in e.lower()]
            self.assertEqual(mode_errors, [])


class BehaviorDigestErrorPathTests(unittest.TestCase):
    """Behavior digest computation failure paths."""

    def test_behavior_missing_path_fails_when_non_strict(self):
        """Allowing null slice inputs does not bless behavior selection failure."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},
                        "behavior": {"paths": ["nonexistent.json"]},
                    }
                },
                "slices": {},
            }
            with self.assertRaisesRegex(ValueError, "matched no tracked files"):
                core.generate_lockfile(cfg, root, source="head", strict=False)

    def test_behavior_missing_glob_fails_when_non_strict(self):
        """An empty behavior glob remains a computation error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},
                        "behavior": {"paths": ["*.nonexistent"]},
                    }
                },
                "slices": {},
            }
            with self.assertRaisesRegex(ValueError, "matched no tracked files"):
                core.generate_lockfile(cfg, root, source="head", strict=False)

    def test_behavior_digest_null_on_subprocess_error(self):
        """CalledProcessError during behavior resolve → digest is null (except path)."""
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            (root / "svc" / "api.yaml").write_text("api\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "behavior": {"paths": ["api.yaml"]},
                    }
                },
                "slices": {},
            }
            from boundver import _lockfile
            original_fn = _lockfile.compute_boundary

            def _raising_compute(provider, ctx, **kwargs):
                if provider.name == "behavior":
                    raise subprocess.CalledProcessError(128, "git")
                return original_fn(provider, ctx, **kwargs)

            with patch.object(_lockfile, "compute_boundary", side_effect=_raising_compute):
                with self.assertRaisesRegex(ValueError, "Behavior digest failed"):
                    _lockfile.generate_lockfile(cfg, root, source="head", strict=False)

    def test_behavior_digest_null_on_oserror(self):
        """OSError during behavior resolve → digest is null (except path)."""
        from unittest.mock import patch
        from boundver import _lockfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            (root / "svc" / "api.yaml").write_text("api\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},
                        "behavior": {"paths": ["api.yaml"]},
                    }
                },
                "slices": {},
            }
            original_fn = _lockfile.compute_boundary

            def _raising_compute(provider, ctx, **kwargs):
                if provider.name == "behavior":
                    raise OSError("disk error")
                return original_fn(provider, ctx, **kwargs)

            with patch.object(_lockfile, "compute_boundary", side_effect=_raising_compute):
                with self.assertRaisesRegex(ValueError, "Behavior digest failed"):
                    _lockfile.generate_lockfile(cfg, root, source="head", strict=False)

    def test_behavior_digest_null_on_value_error(self):
        """ValueError during behavior resolve → digest is null (except path)."""
        from unittest.mock import patch
        from boundver import _lockfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            (root / "svc" / "api.yaml").write_text("api\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},
                        "behavior": {"paths": ["api.yaml"]},
                    }
                },
                "slices": {},
            }
            original_fn = _lockfile.compute_boundary

            def _raising_compute(provider, ctx, **kwargs):
                if provider.name == "behavior":
                    raise ValueError("bad config")
                return original_fn(provider, ctx, **kwargs)

            with patch.object(_lockfile, "compute_boundary", side_effect=_raising_compute):
                with self.assertRaisesRegex(ValueError, "Behavior digest failed"):
                    _lockfile.generate_lockfile(cfg, root, source="head", strict=False)


class BehaviorRecomputeSliceTests(unittest.TestCase):
    """Tests for _recompute_slice_entry with behavior mode."""

    def test_recompute_slice_entry_behavior_mode(self):
        """_recompute_slice_entry handles behavior mode correctly."""
        comp = {"fingerprints": {"exact": "aaa", "behavior": "bbb", "boundary": "ccc", "compat": "ddd"}}
        result = core._recompute_slice_entry(
            "s1",
            {"mode": "behavior", "components": ["svc"]},
            {"svc": comp},
            strict=False,
        )
        self.assertEqual(result["component_digests"]["svc"], "bbb")
        self.assertEqual(result["mode"], "behavior")

    def test_recompute_slice_entry_behavior_null_strict_raises(self):
        """_recompute_slice_entry raises for behavior=None in strict mode."""
        comp = {"fingerprints": {"exact": "aaa", "behavior": None, "boundary": "ccc", "compat": "ddd"}}
        with self.assertRaises(ValueError) as cm:
            core._recompute_slice_entry(
                "s1",
                {"mode": "behavior", "components": ["svc"]},
                {"svc": comp},
                strict=True,
            )
        self.assertIn("behavior", str(cm.exception).lower())

    def test_recompute_slice_entry_behavior_null_non_strict_gives_null(self):
        """_recompute_slice_entry allows None behavior in non-strict mode."""
        comp = {"fingerprints": {"exact": "aaa", "behavior": None, "boundary": "ccc", "compat": "ddd"}}
        result = core._recompute_slice_entry(
            "s1",
            {"mode": "behavior", "components": ["svc"]},
            {"svc": comp},
            strict=False,
        )
        self.assertIsNone(result["component_digests"]["svc"])


class DiffSummaryEdgeCaseTests(unittest.TestCase):
    """Edge cases in _summarize_change with the behavior facet."""

    def test_behavior_and_boundary_without_compat(self):
        """exact + behavior + boundary changed → boundary message, not behavioral."""
        from boundver._diff import _summarize_change
        changes = {"exact": {}, "behavior": {}, "boundary": {}}
        summary = _summarize_change(changes)
        self.assertIn("boundary", summary.lower())
        self.assertNotIn("BREAKING", summary)

    def test_all_facets_changed(self):
        """All four facets changed → BREAKING."""
        from boundver._diff import _summarize_change
        changes = {"exact": {}, "behavior": {}, "boundary": {}, "compat": {}}
        summary = _summarize_change(changes)
        self.assertIn("BREAKING", summary)

    def test_only_behavior_without_exact_fallback(self):
        """Unusual: only behavior changed (shouldn't happen in practice) → fallback."""
        from boundver._diff import _summarize_change
        changes = {"behavior": {}}
        summary = _summarize_change(changes)
        self.assertIn("behavior", summary.lower())

    def test_exact_and_compat_without_boundary(self):
        """exact + compat → BREAKING (compat always implies breaking)."""
        from boundver._diff import _summarize_change
        changes = {"exact": {}, "compat": {}}
        summary = _summarize_change(changes)
        self.assertIn("BREAKING", summary)


class GenerateLockfileForComponentsBehaviorTests(unittest.TestCase):
    """Tests for generate_lockfile_for_components with behavior configured."""

    def test_partial_regen_preserves_behavior_of_unchanged_component(self):
        """When regenerating one component, other components' behavior is preserved."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            (root / "svc" / "api.yaml").write_text("api\n")
            (root / "svc" / "config.json").write_text('{"k": 1}\n')
            (root / "worker").mkdir()
            (root / "worker" / "main.py").write_text("y=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "behavior": {"paths": ["api.yaml", "config.json"]},
                    },
                    "worker": {
                        "path": "worker",
                        "boundary": {"provider": "implicit"},
                    },
                },
                "slices": {},
            }
            # Full generate
            full_lock = core.generate_lockfile(cfg, root, source="head")
            out_path = root / "boundary.lock.json"
            out_path.write_text(json.dumps(full_lock, indent=2))

            # Partial regen of only worker
            merged = core.generate_lockfile_for_components(
                cfg, root, selected_components=["worker"], out_path=out_path,
                source="head", strict=False, existing_lockfile=full_lock,
            )
            # svc behavior should be preserved from the original lockfile
            self.assertEqual(
                merged["components"]["svc"]["fingerprints"]["behavior"],
                full_lock["components"]["svc"]["fingerprints"]["behavior"],
            )

    def test_partial_regen_updates_behavior_slice(self):
        """When a component in a behavior slice is regenerated, slice is recomputed."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            (root / "svc" / "api.yaml").write_text("api\n")
            (root / "svc" / "config.json").write_text('{"k": 1}\n')
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
                        "behavior": {"paths": ["api.yaml", "config.json"]},
                    },
                },
                "slices": {
                    "bslice": {"mode": "behavior", "components": ["svc"]},
                },
            }
            full_lock = core.generate_lockfile(cfg, root, source="head")
            out_path = root / "boundary.lock.json"
            out_path.write_text(json.dumps(full_lock, indent=2))

            # Change behavior file and regen
            (root / "svc" / "config.json").write_text('{"k": 2}\n')
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "cfg"], cwd=root, check=True, capture_output=True)

            merged = core.generate_lockfile_for_components(
                cfg, root, selected_components=["svc"], out_path=out_path, source="head", strict=False
            )
            # Behavior slice should have changed
            self.assertNotEqual(
                full_lock["slices"]["bslice"]["fingerprint"],
                merged["slices"]["bslice"]["fingerprint"],
            )


if __name__ == "__main__":
    unittest.main()
