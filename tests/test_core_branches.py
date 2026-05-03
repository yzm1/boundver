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


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)


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
        """Lines 105-106: semver_major_minor compat mode uses major.minor as compat family."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
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

    def test_semver_major_minor_no_tag_gives_null_compat(self):
        """semver_major_minor with no tag → no version → compat is None."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            cfg = self._setup(root)
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            # No tag → version = None → compat = None
            lockfile = core.generate_lockfile(cfg, root, source="head")
            fp = lockfile["components"]["svc"]["fingerprints"]
            self.assertIsNone(fp["compat"])


class GenerateLockfileUnknownProviderTests(unittest.TestCase):
    """Test unknown boundary provider is handled gracefully."""

    def test_unknown_boundary_provider_recorded_as_error(self):
        """Lines 60-62: unknown provider → boundary_status='error', api_digest=None."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
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
            lockfile = core.generate_lockfile(cfg, root, source="working-tree")
            comp = lockfile["components"]["svc"]
            self.assertEqual(comp["boundary_status"], "error")
            self.assertIsNone(comp["fingerprints"]["boundary"])


class SliceStrictModeTests(unittest.TestCase):
    """Tests for strict-mode errors in slice generation."""

    def _setup(self, root: Path) -> dict:
        (root / "svc").mkdir()
        (root / "svc" / "main.py").write_text("x=1\n")

    def test_compat_slice_strict_raises_when_compat_none(self):
        """Line 167: compat slice with strict=True raises when compat is None."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
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
        """Lines 519-520 (core.py pre-refactor): boundary slice strict raises."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
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
        """Line 170 / 211: unknown slice mode raises ValueError."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
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
        """Line 156-157: if comp_entry is None, digest_parts entry is None."""
        result = core._recompute_slice_entry(
            "s1",
            {"mode": "exact", "components": ["ghost"]},
            {},  # empty components map
            strict=False,
        )
        self.assertIsNone(result["component_digests"]["ghost"])

    def test_recompute_slice_entry_unknown_mode_raises(self):
        """Line 211 / 575 (core.py): unknown mode in _recompute_slice_entry raises."""
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
        """Line 235: generate_lockfile_for_components raises for unknown component."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            cfg = self._setup(root)
            with self.assertRaises(ValueError) as cm:
                core.generate_lockfile_for_components(
                    cfg, root, ["no-such-component"],
                    out_path=root / "boundary.lock.json",
                    source="working-tree",
                )
            self.assertIn("Unknown", str(cm.exception))

    def test_creates_fresh_lockfile_when_none_exists(self):
        """Line 247: generate_lockfile_for_components starts fresh when out_path missing."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            cfg = self._setup(root)
            out_path = root / "boundary.lock.json"
            self.assertFalse(out_path.exists())
            result = core.generate_lockfile_for_components(
                cfg, root, ["svc"],
                out_path=out_path,
                source="working-tree",
            )
            self.assertIn("svc", result["components"])

    def test_merges_into_existing_lockfile(self):
        """generate_lockfile_for_components merges into an existing lockfile."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
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
    """Test vendored copy drift warning in verify_lockfile."""

    def test_vendored_drift_recorded_as_issue(self):
        """Line 347: vendored drift appears in verify_lockfile issues."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
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
            lockfile = core.generate_lockfile(cfg, root, source="working-tree")
            # Lockfile itself has warnings already; verify adds them as issues
            issues = core.verify_lockfile(cfg, lockfile, root, source="working-tree")
            # The lockfile was generated WITH drift, so exact fingerprints match.
            # But there should still be a VENDORED DRIFT warning in issues.
            drift_issues = [i for i in issues if "VENDORED DRIFT" in i]
            self.assertGreater(len(drift_issues), 0)


class ValidateConfigBranchTests(unittest.TestCase):
    """Tests for validate_config edge cases."""

    def test_validate_config_boundary_path_not_found_reports_error(self):
        """Lines 761-762: boundary path that doesn't exist produces an error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
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

    def test_validate_config_boundary_slice_with_implicit_provider_errors(self):
        """Lines 820-828: boundary slice with implicit/leaf component reports error."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
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
            self.assertTrue(any("boundary" in e.lower() for e in errors))


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

    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def test_explain_component_with_boundary_paths_and_changes(self):
        """Lines 1108-1110: explain shows boundary-relevant changed files."""
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
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
        """Lines 1116: explain shows 'Boundary-relevant changed files: none' when no boundary changes."""
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
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

    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def test_implicit_provider_with_paths_produces_boundary_digest(self):
        """Line 224: ImplicitProvider delegates to PathHashProvider when paths declared."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
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

    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def test_leaf_provider_validate_config_returns_empty(self):
        """Line 269: LeafProvider.validate_config returns []."""
        from boundver.providers import LeafProvider
        result = LeafProvider().validate_config({}, "svc", Path("."))
        self.assertEqual(result, [])

    def test_leaf_provider_explain_diff_returns_string(self):
        """Line 277: LeafProvider.explain_diff returns message."""
        from boundver.providers import LeafProvider
        result = LeafProvider().explain_diff(None, None, None)
        self.assertIsInstance(result, str)
        self.assertIn("leaf", result.lower())

    def test_leaf_provider_in_lockfile(self):
        """Leaf provider generates no boundary digest but status is 'ok'."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
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

    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def test_path_hash_provider_validate_config_missing_path(self):
        """Lines 188-196: PathHashProvider.validate_config reports missing paths."""
        from boundver.providers import PathHashProvider
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            # api.yaml does not exist
            boundary_cfg = {"paths": ["api.yaml"]}
            errors = PathHashProvider().validate_config(boundary_cfg, "svc", root)
            self.assertTrue(any("api.yaml" in e or "not found" in e.lower() for e in errors))

    def test_path_hash_provider_explain_diff(self):
        """Line 204: PathHashProvider.explain_diff returns appropriate string."""
        from boundver.providers import PathHashProvider
        result = PathHashProvider().explain_diff(None, None, None)
        self.assertIsInstance(result, str)

    def test_implicit_provider_explain_diff(self):
        """Line 245: ImplicitProvider.explain_diff returns appropriate string."""
        from boundver.providers import ImplicitProvider
        result = ImplicitProvider().explain_diff(None, None, None)
        self.assertIsInstance(result, str)
        self.assertIn("implicit", result.lower())


class ValidateConfigMiscBranchTests(unittest.TestCase):
    """Additional validate_config branches for coverage."""

    def test_load_config_schema_invalid_json(self):
        """_load_config_schema returns None when schema file is invalid JSON."""
        from boundver._config import _load_config_schema
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "boundary.config.schema.json").write_text("not valid json {{{")
            result = _load_config_schema(root)
            self.assertIsNone(result)

    def test_validate_config_non_dict_returns_error(self):
        """validate_config with non-dict input returns an error immediately."""
        errors = core.validate_config("not a dict", Path("/tmp"))
        self.assertEqual(errors, ["Config root must be a JSON object"])

    def test_validate_config_boundary_path_escapes_component_root(self):
        """Boundary path containing '..' that escapes component root is rejected."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
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
            _init_git_repo(root)
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
            _init_git_repo(root)
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
            _init_git_repo(root)
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
            self.assertTrue(any("escapes" in e for e in errors))


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
    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def test_generate_dry_run_does_not_write_lockfile(self):
        import io, sys as _sys
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
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
                _sys.argv = ["boundver", "generate", "--dry-run"]
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
    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def test_verify_drifted_outputs_red_text(self):
        """verify command prints a LOCKFILE OUT OF DATE header when drift is found."""
        import io, sys as _sys
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x=1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "implicit", "paths": []}}},
                "slices": {},
            }
            # Generate lockfile at commit 1
            lockfile = core.generate_lockfile(cfg, root)
            # Now make a second commit so HEAD diverges from lockfile
            (root / "svc" / "main.py").write_text("x=2\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "change"], cwd=root, check=True, capture_output=True)
            (root / "boundary.config.json").write_text(json.dumps(cfg))
            (root / "boundary.lock.json").write_text(json.dumps(lockfile))
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
    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def test_explain_unknown_component_exits_2(self):
        import sys as _sys
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
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
    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def test_init_creates_config_with_components_key(self):
        """boundver init writes boundary.config.json with a components block."""
        import sys as _sys
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
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
    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def test_explain_no_boundary_paths_returns_early(self):
        """explain_component_changes returns 0 and notes no boundary paths."""
        import io
        from contextlib import redirect_stdout
        from boundver.core import explain_component_changes
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
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
            self._init_git_repo(root)
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
    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def test_why_shows_version_when_present(self):
        """why_component prints Version: line when lockfile has a version."""
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.yaml").write_text("openapi: 3.0.0\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "project": "p",
                "components": {"svc": {"path": "svc", "boundary": {"provider": "openapi", "paths": ["api.yaml"]}}},
                "slices": {},
            }
            lockfile = core.generate_lockfile(cfg, root)
            # Manually inject a version
            lockfile["components"]["svc"]["version"] = "1.0.0"
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
    """Validate provider declared in component but not in providers list."""

    def test_custom_provider_in_component_not_in_providers_list(self):
        """Component uses custom.X but providers list doesn't include it (lines 234-235)."""
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
            self.assertTrue(
                any("not declared in the" in e for e in errors),
                f"Expected 'not declared in the' error, got: {errors}",
            )


class ExplainGitDiffFailureTests(unittest.TestCase):
    """explain_component_changes returns exit 2 when git diff fails."""

    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def test_explain_returns_2_when_git_diff_fails(self):
        """explain_component_changes returns 2 when the git diff subprocess fails (lines 150-152)."""
        from unittest.mock import patch
        import subprocess as _sp
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
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
        """explain_component_changes with source='index' exercises the --cached diff path (_output.py:142)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
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
        """Diff output lines with no tab are silently skipped (_output.py:158)."""
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

    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def _base_fixtures(self, root: Path):
        """Create minimal committed state and return (config, lockfile)."""
        self._init_git_repo(root)
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
        """why_component returns 2 when generate_lockfile throws (_output.py:264-266)."""
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg, lockfile = self._base_fixtures(root)
            # Patch at the source module so the lazy import inside why_component gets the mock.
            with patch("boundver._lockfile.generate_lockfile", side_effect=RuntimeError("boom")):
                rc = core.why_component(cfg, lockfile, root, "svc")
            self.assertEqual(rc, 2)

    def test_why_component_git_diff_failure_is_graceful(self):
        """why_component continues when git diff fails (_output.py:327-328).

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
    """_lockfile.py line 78: working-tree full.read_bytes() path in _make_read_file."""

    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def test_working_tree_boundary_reads_file_bytes(self):
        """generate_lockfile with working-tree source and explicit boundary paths reads files
        via full.read_bytes() (_lockfile.py:80-81)."""
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
        """Changing a boundary file on disk changes the boundary digest (confirms line 80 is active)."""
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
        """generate_lockfile with source='index' reads boundary files via git cat-file
        (_lockfile.py:78 — the index branch of _make_read_file)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
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
        """generate_lockfile reads version from a file on disk for working-tree source
        (_lockfile.py:52 — the working-tree branch of _version_read_file)."""
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
        """generate_lockfile reads version from staged file for index source
        (_lockfile.py:50 — the index branch of _version_read_file)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
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
    """_lockfile.py lines 162-163: slice references a component absent from lockfile."""

    def test_slice_component_not_in_lockfile_gets_null_digest(self):
        """When a slice lists a component name not present in the lockfile components dict,
        the component is assigned None and the slice digest reflects that (_lockfile.py:162-163)."""
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
    """_git.py lines 135-139: list_head_files cat-file fallback for a single committed file."""

    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def test_list_head_files_returns_single_file_path(self):
        """list_head_files with a path pointing at a single file (blob) falls back to cat-file -t
        and returns the single file path (_git.py:135-139)."""
        from boundver._git import list_head_files
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "readme.txt").write_text("hello\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            # Passing a single file path — ls-tree returns nothing for a blob, so cat-file -t fallback runs.
            result = list_head_files(root, "readme.txt")
            self.assertEqual(result, ["readme.txt"])

    def test_list_head_files_cat_file_fallback_via_mock(self):
        """When ls-tree returns empty output, cat-file -t is tried (lines 135-139).
        Uses mock to force ls-tree to return empty so the fallback branch is taken."""
        from unittest.mock import patch, MagicMock
        from boundver._git import list_head_files

        call_count = [0]

        def fake_git_run(repo_root, args, **kw):
            call_count[0] += 1
            result = MagicMock()
            if "ls-tree" in args:
                result.stdout = ""  # Empty → triggers cat-file fallback
            else:
                # cat-file -t → returns "blob"
                result.stdout = "blob"
            return result

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch("boundver._git._git_run", side_effect=fake_git_run):
                result = list_head_files(root, "readme.txt")
            self.assertEqual(result, ["readme.txt"])
            self.assertEqual(call_count[0], 2)  # ls-tree + cat-file

    def test_list_head_files_cat_file_not_blob_returns_empty(self):
        """When cat-file -t returns 'tree' (not 'blob'), returns [] (line 139 false branch)."""
        from unittest.mock import patch, MagicMock
        from boundver._git import list_head_files

        def fake_git_run(repo_root, args, **kw):
            result = MagicMock()
            if "ls-tree" in args:
                result.stdout = ""
            else:
                result.stdout = "tree"  # Not a blob
            return result

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch("boundver._git._git_run", side_effect=fake_git_run):
                result = list_head_files(root, "somedir")
            self.assertEqual(result, [])

    def test_list_head_files_cat_file_raises_returns_empty(self):
        """When cat-file -t raises CalledProcessError, returns [] (_git.py:137-138)."""
        from unittest.mock import patch, MagicMock
        import subprocess as _sp
        from boundver._git import list_head_files

        call_count = [0]

        def fake_git_run(repo_root, args, **kw):
            call_count[0] += 1
            if "ls-tree" in args:
                result = MagicMock()
                result.stdout = ""
                return result
            # cat-file -t → raises
            raise _sp.CalledProcessError(128, "git")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch("boundver._git._git_run", side_effect=fake_git_run):
                result = list_head_files(root, "missingfile.txt")
            self.assertEqual(result, [])


class DetectProviderTests(unittest.TestCase):
    """Tests for _config._detect_provider — covers lines 367, 371, 376, 380, 385, 387."""

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
        """File starting with 'openapi' but not in the exact-name list → fallback glob (line 367)."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "openapi-v2.yaml").write_text("openapi: 2.0\n")
            provider, paths = self._fn(d)
            self.assertEqual(provider, "openapi")

    def test_detects_boundary_json(self):
        """boundary.json → provider 'json-file' (line 371)."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "boundary.json").write_text("{}")
            provider, paths = self._fn(d)
            self.assertEqual(provider, "json-file")
            self.assertIn("boundary.json", paths)

    def test_detects_python_exports(self):
        """__init__.py → provider 'python-exports' (line 376)."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "__init__.py").write_text("")
            provider, paths = self._fn(d)
            self.assertEqual(provider, "python-exports")

    def test_detects_typescript_src_index(self):
        """src/index.ts → provider 'typescript-exports' (line 380)."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "src").mkdir()
            (d / "src" / "index.ts").write_text("")
            provider, paths = self._fn(d)
            self.assertEqual(provider, "typescript-exports")
            self.assertIn("src/index.ts", paths)

    def test_detects_typescript_root_index(self):
        """index.ts at root → provider 'typescript-exports' (line 385)."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "index.ts").write_text("")
            provider, paths = self._fn(d)
            self.assertEqual(provider, "typescript-exports")
            self.assertIn("index.ts", paths)

    def test_falls_back_to_implicit(self):
        """No recognisable files → provider 'implicit' (line 387)."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "random.txt").write_text("hello")
            provider, paths = self._fn(d)
            self.assertEqual(provider, "implicit")
            self.assertEqual(paths, [])


class HashingContentOnlyValueErrorTests(unittest.TestCase):
    """_hashing.py:125-126,135-136: ValueError fallback when file not under base path."""

    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def test_content_only_digest_file_outside_base_uses_repo_rel(self):
        """When a file returned by _list_files_for_source is not relative to base,
        the ValueError fallback uses the full repo-relative path (_hashing.py:135-136)."""
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

    def test_content_only_digest_head_file_outside_base_uses_repo_rel(self):
        """Same ValueError fallback for head source (_hashing.py:125-126)."""
        from unittest.mock import patch, MagicMock
        from boundver._hashing import _content_only_digest

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "readme.txt").write_text("hello\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)

            # Patch _list_files_for_source to return "readme.txt" while base is "svc".
            with patch("boundver._hashing._list_files_for_source", return_value=["readme.txt"]):
                digest = _content_only_digest(root, "svc", source="head")
            self.assertIsNotNone(digest)


class PathHashProviderGlobTests(unittest.TestCase):
    """providers.py:211: fnmatch glob pattern expansion in PathHashProvider."""

    def test_glob_pattern_expands_matching_files(self):
        """A boundary path with '*' expands against component files (providers.py:211)."""
        from boundver.providers import PathHashProvider, ProviderContext
        from unittest.mock import MagicMock

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
    """_git.py lines 206, 208-209: git_latest_tag fallback when describe fails."""

    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def test_latest_tag_fallback_returns_tag_when_describe_fails(self):
        """When git describe fails but git tag --list succeeds, returns the tag version
        (_git.py:206)."""
        from unittest.mock import patch, call
        import subprocess as _sp
        from boundver._git import git_latest_tag

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)

            call_count = [0]
            def fake_git_run(repo_root, args, **kw):
                call_count[0] += 1
                if "describe" in args:
                    raise _sp.CalledProcessError(128, "git")
                # Simulate `git tag --list svc-v* --sort=-v:refname`
                result = MagicMock()
                result.stdout = "svc-v2.1.0\nsvc-v1.0.0\n"
                return result

            from unittest.mock import MagicMock
            with patch("boundver._git._git_run", side_effect=fake_git_run):
                version = git_latest_tag(root, "svc-v")
            self.assertEqual(version, "2.1.0")

    def test_latest_tag_fallback_returns_none_when_no_tags(self):
        """When both describe and tag list return nothing, returns None (_git.py:208-209)."""
        from unittest.mock import patch
        import subprocess as _sp
        from boundver._git import git_latest_tag

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)

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
        """When both describe and tag --list fail, returns None (_git.py:208-209 outer except)."""
        from unittest.mock import patch
        import subprocess as _sp
        from boundver._git import git_latest_tag

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)

            def fake_git_run(repo_root, args, **kw):
                raise _sp.CalledProcessError(128, "git")

            with patch("boundver._git._git_run", side_effect=fake_git_run):
                version = git_latest_tag(root, "svc-v")
            self.assertIsNone(version)


if __name__ == "__main__":
    unittest.main()

