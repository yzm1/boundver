"""Regression tests for contracts rediscovered in the 0.10 first-principles audit.

These tests intentionally exercise the public behavior at more than one layer.
That keeps path selection, lock generation, verification, and CLI policy from
quietly acquiring different interpretations of the same configuration.
"""

from __future__ import annotations

import builtins
import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import patch

import boundver
import boundver.core as core
from boundver._config import _expand_component_paths, discover_components, validate_config
from boundver._output import analyze_explain_changes
from boundver._utils import ConfigError
from boundver.providers import (
    JsonCanonicalProvider,
    OpenApiCanonicalProvider,
    PathHashProvider,
    ProviderContext,
)
from tests._repo_fixtures import commit_all as _commit_all
from tests._repo_fixtures import init_git_repo as _init_git_repo


def _provider_context(pattern: str, files: Dict[str, bytes]) -> ProviderContext:
    def read_file(repo_rel: str) -> bytes:
        return files[repo_rel]

    def list_files(prefix: str) -> List[str]:
        prefix = prefix.rstrip("/")
        return sorted(
            path for path in files if path == prefix or path.startswith(prefix + "/")
        )

    return ProviderContext(
        repo_root=Path("/repo"),
        component_path="svc",
        boundary_cfg={"paths": [pattern]},
        source="working-tree",
        read_file=read_file,
        list_files=list_files,
    )


def _run_cli(*args: str, repo_root: Path) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = 0
    with patch.object(sys, "argv", ["boundver", *args]), patch(
        "boundver.core.git_root", return_value=repo_root
    ):
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                core.main()
        except SystemExit as exc:
            exit_code = int(exc.code) if exc.code is not None else 0
    return exit_code, stdout.getvalue(), stderr.getvalue()


class SegmentAwareGlobTests(unittest.TestCase):
    def test_star_question_and_character_classes_do_not_cross_directories(self):
        files = {
            "svc/api/v1.yaml": b"one",
            "svc/api/vA.yaml": b"letter",
            "svc/api/v10.yaml": b"ten",
            "svc/api/nested/v2.yaml": b"nested",
            "svc/api/a.json": b"{}",
            "svc/api/b.json": b"{}",
            "svc/api/c.json": b"{}",
        }

        star = PathHashProvider().resolve(_provider_context("api/*.yaml", files))
        self.assertEqual(star.status, "ok", star.errors)
        self.assertEqual(
            [label for label, _ in star.entries],
            ["file:api/v1.yaml", "file:api/v10.yaml", "file:api/vA.yaml"],
        )

        question = PathHashProvider().resolve(
            _provider_context("api/v?.yaml", files)
        )
        self.assertEqual(question.status, "ok", question.errors)
        self.assertEqual(
            [label for label, _ in question.entries],
            ["file:api/v1.yaml", "file:api/vA.yaml"],
        )

        character_class = PathHashProvider().resolve(
            _provider_context("api/[ab].json", files)
        )
        self.assertEqual(character_class.status, "ok", character_class.errors)
        self.assertEqual(
            [label for label, _ in character_class.entries],
            ["file:api/a.json", "file:api/b.json"],
        )

    def test_double_star_matches_zero_or_more_directory_segments(self):
        files = {
            "svc/root.yaml": b"root",
            "svc/api/direct.yaml": b"direct",
            "svc/api/nested/deep.yaml": b"deep",
            "svc/api/nested/ignore.json": b"{}",
        }

        shallow = PathHashProvider().resolve(_provider_context("*.yaml", files))
        self.assertEqual(shallow.status, "ok", shallow.errors)
        self.assertEqual(
            [label for label, _ in shallow.entries],
            ["file:root.yaml"],
        )

        from_root = PathHashProvider().resolve(
            _provider_context("**/*.yaml", files)
        )
        self.assertEqual(from_root.status, "ok", from_root.errors)
        self.assertEqual(
            [label for label, _ in from_root.entries],
            [
                "file:api/direct.yaml",
                "file:api/nested/deep.yaml",
                "file:root.yaml",
            ],
        )

        below_api = PathHashProvider().resolve(
            _provider_context("api/**/*.yaml", files)
        )
        self.assertEqual(below_api.status, "ok", below_api.errors)
        self.assertEqual(
            [label for label, _ in below_api.entries],
            ["file:api/direct.yaml", "file:api/nested/deep.yaml"],
        )

    def test_canonical_providers_use_the_same_glob_contract(self):
        files = {
            "svc/contracts/direct.json": b'{"openapi":"3.0.0","paths":{}}',
            "svc/contracts/nested/deep.json": b'{"openapi":"3.0.0","paths":{}}',
            "svc/other.json": b"{}",
        }

        for provider in (
            JsonCanonicalProvider(),
            OpenApiCanonicalProvider(),
        ):
            with self.subTest(provider=provider.name):
                shallow = provider.resolve(
                    _provider_context("contracts/*.json", files)
                )
                self.assertEqual(shallow.status, "ok", shallow.errors)
                self.assertEqual(
                    [label for label, _ in shallow.entries],
                    ["canonical:contracts/direct.json"],
                )

                result = provider.resolve(
                    _provider_context("contracts/**/*.json", files)
                )
                self.assertEqual(result.status, "ok", result.errors)
                self.assertEqual(
                    [label for label, _ in result.entries],
                    [
                        "canonical:contracts/direct.json",
                        "canonical:contracts/nested/deep.json",
                    ],
                )

    def test_config_expansion_uses_the_provider_glob_contract(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc" / "api" / "nested").mkdir(parents=True)
            (root / "svc" / "root.yaml").write_text("root\n", encoding="utf-8")
            (root / "svc" / "api" / "direct.yaml").write_text(
                "direct\n", encoding="utf-8"
            )
            (root / "svc" / "api" / "nested" / "deep.yaml").write_text(
                "deep\n", encoding="utf-8"
            )

            self.assertEqual(
                _expand_component_paths(root, "svc", ["*.yaml"]),
                {"root.yaml"},
            )
            self.assertEqual(
                _expand_component_paths(root, "svc", ["**/*.yaml"]),
                {"root.yaml", "api/direct.yaml", "api/nested/deep.yaml"},
            )
            self.assertEqual(
                _expand_component_paths(root, "svc", ["api/*.yaml"]),
                {"api/direct.yaml"},
            )

    def test_explain_uses_the_provider_glob_contract(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "svc" / "api" / "nested").mkdir(parents=True)
            direct = root / "svc" / "api" / "direct.yaml"
            nested = root / "svc" / "api" / "nested" / "deep.yaml"
            direct.write_text("before\n", encoding="utf-8")
            nested.write_text("before\n", encoding="utf-8")
            _commit_all(root)
            direct.write_text("after\n", encoding="utf-8")
            nested.write_text("after\n", encoding="utf-8")

            base_config = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {
                            "provider": "openapi",
                            "paths": ["api/*.yaml"],
                        },
                    }
                },
                "slices": {},
            }
            direct_only = analyze_explain_changes(
                base_config, root, "svc", base_ref="HEAD", source="working-tree"
            )
            self.assertIsNone(direct_only["error"])
            self.assertEqual(
                direct_only["boundary_changed"],
                [("M", "svc/api/direct.yaml")],
            )

            recursive_config = copy.deepcopy(base_config)
            recursive_config["components"]["svc"]["boundary"]["paths"] = [
                "api/**/*.yaml"
            ]
            recursive = analyze_explain_changes(
                recursive_config,
                root,
                "svc",
                base_ref="HEAD",
                source="working-tree",
            )
            self.assertIsNone(recursive["error"])
            self.assertEqual(
                recursive["boundary_changed"],
                [
                    ("M", "svc/api/direct.yaml"),
                    ("M", "svc/api/nested/deep.yaml"),
                ],
            )

    def test_explain_counts_the_removed_side_of_a_boundary_rename(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            source = root / "svc" / "api" / "contract.yaml"
            source.parent.mkdir(parents=True)
            source.write_text("before\n", encoding="utf-8")
            _commit_all(root)
            destination = root / "svc" / "internal" / "contract.txt"
            destination.parent.mkdir()
            subprocess.run(
                ["git", "mv", str(source.relative_to(root)), str(destination.relative_to(root))],
                cwd=root,
                check=True,
                capture_output=True,
            )
            config = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {
                            "provider": "json-file",
                            "paths": ["api/*.yaml"],
                        },
                    }
                },
                "slices": {},
            }

            result = analyze_explain_changes(
                config, root, "svc", base_ref="HEAD", source="working-tree"
            )

            self.assertIsNone(result["error"])
            self.assertIn(
                ("R100", "svc/api/contract.yaml"),
                result["boundary_changed"],
            )


class VendoredCopyInvariantTests(unittest.TestCase):
    @staticmethod
    def _config() -> dict:
        return {
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

    def _repo(self, root: Path, *, vendor: Optional[str]) -> None:
        _init_git_repo(root)
        (root / "svc").mkdir()
        (root / "svc" / "contract.txt").write_text("same\n", encoding="utf-8")
        if vendor is not None:
            (root / "vendor" / "svc").mkdir(parents=True)
            (root / "vendor" / "svc" / "contract.txt").write_text(
                vendor, encoding="utf-8"
            )
        _commit_all(root)

    def test_strict_generation_rejects_a_missing_vendored_copy_in_every_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root, vendor=None)
            for source in ("head", "index", "working-tree"):
                with self.subTest(source=source):
                    with self.assertRaisesRegex(ConfigError, "vendor/svc|vendored"):
                        core.generate_lockfile(
                            self._config(), root, source=source, strict=True
                        )

    def test_strict_generation_rejects_a_divergent_vendored_copy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root, vendor="different\n")
            for source in ("head", "index", "working-tree"):
                with self.subTest(source=source):
                    with self.assertRaisesRegex(ConfigError, "vendor/svc|vendored|differs"):
                        core.generate_lockfile(
                            self._config(), root, source=source, strict=True
                        )

    def test_every_strict_generated_lock_immediately_verifies(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo(root, vendor="same\n")
            for source in ("head", "index", "working-tree"):
                with self.subTest(source=source):
                    lockfile = core.generate_lockfile(
                        self._config(), root, source=source, strict=True
                    )
                    self.assertEqual(
                        core.verify_lockfile(
                            self._config(), lockfile, root, source=source
                        ),
                        [],
                    )


class FacetInvariantTests(unittest.TestCase):
    def test_containment_validation_uses_the_selected_tracked_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "svc").mkdir()
            tracked = root / "svc" / "api.json"
            tracked.write_text("{}\n", encoding="utf-8")
            _commit_all(root)
            config = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {
                            "provider": "json-file",
                            "paths": ["**/*.json"],
                        },
                        "behavior": {"paths": ["api.json"]},
                    }
                },
                "slices": {},
            }

            # The untracked file is outside the working-tree hash view and must
            # not invent a containment violation.
            (root / "svc" / "untracked.json").write_text(
                "{}\n", encoding="utf-8"
            )
            self.assertEqual(
                validate_config(config, root, source="working-tree"), []
            )

            # HEAD validation remains tied to the committed snapshot even when
            # the corresponding file is absent from disk.
            tracked.unlink()
            self.assertEqual(validate_config(config, root, source="head"), [])

    def test_behavior_changes_whenever_boundary_changes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "svc").mkdir()
            schema = root / "svc" / "schema.json"
            schema.write_text(
                '{"type":"object","properties":{"id":{"type":"string"}}}\n',
                encoding="utf-8",
            )
            (root / "svc" / "runtime.json").write_text("{}\n", encoding="utf-8")
            _commit_all(root)
            config = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {
                            "provider": "json-canonical",
                            "paths": ["schema.json"],
                        },
                        # An omission here must not violate behavior >= boundary.
                        "behavior": {"paths": ["runtime.json"]},
                    }
                },
                "slices": {},
            }

            before = core.generate_lockfile(
                config, root, source="working-tree", strict=True
            )
            schema.write_text(
                '{"type":"object","properties":{"id":{"type":"integer"}}}\n',
                encoding="utf-8",
            )
            after = core.generate_lockfile(
                config, root, source="working-tree", strict=True
            )
            old_fingerprints = before["components"]["svc"]["fingerprints"]
            new_fingerprints = after["components"]["svc"]["fingerprints"]
            self.assertNotEqual(
                old_fingerprints["boundary"], new_fingerprints["boundary"]
            )
            self.assertNotEqual(
                old_fingerprints["behavior"], new_fingerprints["behavior"]
            )


class ScopedUpdateInvariantTests(unittest.TestCase):
    def test_scoped_update_never_rewrites_a_stale_unselected_component(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            for name in ("a", "b"):
                (root / name).mkdir()
                (root / name / "main.txt").write_text(
                    f"{name}-before\n", encoding="utf-8"
                )
            config = {
                "project": "p",
                "components": {
                    name: {
                        "path": name,
                        "boundary": {"provider": "implicit"},
                    }
                    for name in ("a", "b")
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
            _commit_all(root)
            original = core.generate_lockfile(
                config, root, source="working-tree", strict=True
            )
            lock_path = root / "boundary.lock.json"
            lock_path.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")

            (root / "a" / "main.txt").write_text("a-after\n", encoding="utf-8")
            (root / "b" / "main.txt").write_text("b-after\n", encoding="utf-8")
            exit_code, stdout, stderr = _run_cli(
                "verify",
                "--source",
                "working-tree",
                "--components",
                "a",
                "--update",
                "--format",
                "json",
                repo_root=root,
            )

            updated = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertIn(exit_code, {core.EXIT_OK, core.EXIT_USAGE}, stderr)
            if exit_code == core.EXIT_OK:
                self.assertNotEqual(
                    updated["components"]["a"], original["components"]["a"]
                )
            else:
                self.assertTrue(
                    "stale" in stderr.lower()
                    or "unselected" in stderr.lower()
                    or "partial" in stderr.lower(),
                    stdout + stderr,
                )
            self.assertEqual(
                updated["components"]["b"],
                original["components"]["b"],
                "--components a --update must not silently accept drift in b",
            )

    def test_scoped_update_does_not_use_global_preflight_repair(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            for name in ("a", "b"):
                (root / name).mkdir()
                (root / name / "main.txt").write_text(
                    f"{name}-before\n", encoding="utf-8"
                )
            config = {
                "project": "p",
                "components": {
                    name: {
                        "path": name,
                        "boundary": {"provider": "implicit"},
                    }
                    for name in ("a", "b")
                },
                "slices": {},
            }
            config_path = root / "boundary.config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            _commit_all(root)
            original = core.generate_lockfile(
                config, root, source="working-tree", strict=True
            )
            lock_path = root / "boundary.lock.json"
            lock_path.write_text(
                json.dumps(original, indent=2) + "\n", encoding="utf-8"
            )
            before_bytes = lock_path.read_bytes()

            (root / "b" / "main.txt").write_text("b-after\n", encoding="utf-8")
            config["slices"] = {
                "all": {"mode": "exact", "components": ["a", "b"]}
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")

            exit_code, stdout, stderr = _run_cli(
                "verify",
                "--source",
                "working-tree",
                "--components",
                "a",
                "--update",
                "--format",
                "json",
                repo_root=root,
            )

            self.assertEqual(exit_code, core.EXIT_USAGE, stdout + stderr)
            payload = json.loads(stdout)
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["updated"])
            self.assertTrue(
                any("scoped --update" in issue for issue in payload["issues"]),
                payload,
            )
            self.assertEqual(lock_path.read_bytes(), before_bytes)


class ConfigWithoutJsonschemaTests(unittest.TestCase):
    @staticmethod
    def _minimal_config() -> dict:
        return {
            "project": "p",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "implicit"},
                }
            },
            "slices": {},
        }

    def _validate_without_jsonschema(self, config: dict, root: Path) -> List[str]:
        real_import = builtins.__import__

        def import_without_jsonschema(name, *args, **kwargs):
            if name == "jsonschema" or name.startswith("jsonschema."):
                raise ImportError("jsonschema deliberately unavailable in this test")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_without_jsonschema):
            return validate_config(config, root, source="head")

    def test_unknown_fields_are_rejected_without_the_optional_schema_engine(self):
        cases = {}

        top = self._minimal_config()
        top["projet"] = "typo"
        cases["projet"] = top

        defaults = self._minimal_config()
        defaults["defaults"] = {"compat_mode": "major", "verify_facet": ["exact"]}
        cases["verify_facet"] = defaults

        component = self._minimal_config()
        component["components"]["svc"]["consumerz"] = []
        cases["consumerz"] = component

        boundary = self._minimal_config()
        boundary["components"]["svc"]["boundary"]["pathz"] = []
        cases["pathz"] = boundary

        behavior = self._minimal_config()
        behavior["components"]["svc"]["behavior"] = {
            "paths": [],
            "artifactz": [],
        }
        cases["artifactz"] = behavior

        version_source = self._minimal_config()
        version_source["components"]["svc"]["version_source"] = {
            "git_tag_prefix": "svc-v",
            "tag_prefx": "svc-v",
        }
        cases["tag_prefx"] = version_source

        slice_config = self._minimal_config()
        slice_config["slices"] = {
            "all": {
                "mode": "exact",
                "components": ["svc"],
                "componentz": ["svc"],
            }
        }
        cases["componentz"] = slice_config

        provider = self._minimal_config()
        provider["providers"] = [
            {
                "module": "example_provider",
                "class": "ExampleProvider",
                "module_path": "typo",
            }
        ]
        cases["module_path"] = provider

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for unknown_field, config in cases.items():
                with self.subTest(field=unknown_field):
                    errors = self._validate_without_jsonschema(config, root)
                    self.assertTrue(
                        any(unknown_field in error for error in errors),
                        f"{unknown_field!r} was silently accepted: {errors}",
                    )

    def test_repository_schema_cannot_replace_the_installed_contract(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "boundary.config.schema.json").write_text(
                "{}\n", encoding="utf-8"
            )
            config = self._minimal_config()
            config["unexpected"] = True

            errors = validate_config(config, root, source="head")

            self.assertTrue(
                any("unexpected" in error for error in errors), errors
            )

    def test_unique_facets_and_custom_provider_names_do_not_need_jsonschema(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            duplicate_facets = self._minimal_config()
            duplicate_facets["defaults"] = {
                "verify_facets": ["exact", "exact"]
            }
            errors = self._validate_without_jsonschema(duplicate_facets, root)
            self.assertTrue(any("contains duplicates" in error for error in errors), errors)

            for invalid_name in ("custom.", " custom.named", "custom.named ", 7):
                with self.subTest(name=invalid_name):
                    provider = self._minimal_config()
                    provider["providers"] = [
                        {
                            "module": "example_provider",
                            "class": "ExampleProvider",
                            "name": invalid_name,
                        }
                    ]
                    errors = self._validate_without_jsonschema(provider, root)
                    self.assertTrue(
                        any("field 'name'" in error for error in errors), errors
                    )

    def test_component_path_must_be_a_tracked_directory_in_head_and_index(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "README.md").write_text("not a component tree\n", encoding="utf-8")
            _commit_all(root)
            config = {
                "project": "p",
                "components": {
                    "bad": {
                        "path": "README.md",
                        "boundary": {"provider": "implicit"},
                    }
                },
                "slices": {},
            }

            for source in ("head", "index"):
                with self.subTest(source=source):
                    errors = validate_config(config, root, source=source)
                    self.assertTrue(
                        any("tracked directory" in error for error in errors),
                        errors,
                    )


class ReadmeConfigurationTests(unittest.TestCase):
    def test_readme_example_uses_typed_internal_and_external_consumers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payment = root / "services" / "payment"
            (payment / "openapi").mkdir(parents=True)
            (payment / "config").mkdir()
            (payment / "package.json").write_text(
                '{"version":"1.0.0"}\n', encoding="utf-8"
            )
            (payment / "openapi" / "payment.yaml").write_text(
                "openapi: 3.0.0\npaths: {}\n", encoding="utf-8"
            )
            (payment / "config" / "defaults.json").write_text(
                "{}\n", encoding="utf-8"
            )
            checkout = root / "apps" / "checkout"
            checkout.mkdir(parents=True)
            (checkout / "package.json").write_text(
                '{"version":"1.0.0"}\n', encoding="utf-8"
            )
            readme = (Path(__file__).parents[1] / "README.md").read_text(
                encoding="utf-8"
            )
            practical = readme.split("## A practical configuration", 1)[1]
            json_block = practical.split("```json", 1)[1].split("```", 1)[0]
            readme_config = json.loads(json_block)

            self.assertEqual(validate_config(readme_config, root), [])
            self.assertIn(
                "checkout-web",
                readme_config["components"]["payment-api"]["consumers"],
            )
            self.assertIn(
                "external-risk-service",
                readme_config["components"]["payment-api"]["external_consumers"],
            )
            lockfile = core.generate_lockfile(
                readme_config, root, source="working-tree", strict=True
            )
            self.assertEqual(lockfile["components"]["payment-api"]["boundary_status"], "ok")
            self.assertIsNotNone(
                lockfile["components"]["payment-api"]["fingerprints"]["boundary"]
            )


class OnboardingInvariantTests(unittest.TestCase):
    def test_readme_one_minute_path_runs_in_a_tracked_src_repository(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "src" / "demo").mkdir(parents=True)
            (root / "src" / "demo" / "main.py").write_text(
                "value = 1\n", encoding="utf-8"
            )
            _commit_all(root)

            for args in (
                ("init",),
                ("validate-config",),
                ("generate", "--source", "working-tree"),
                (
                    "verify",
                    "--source",
                    "working-tree",
                    "--facets",
                    "exact",
                ),
            ):
                exit_code, _stdout, stderr = _run_cli(*args, repo_root=root)
                self.assertEqual(exit_code, core.EXIT_OK, (args, stderr))

    def test_root_python_manifest_maps_to_a_safe_nested_package(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "demo"\nversion = "1.2.3"\n',
                encoding="utf-8",
            )
            (root / "src" / "demo").mkdir(parents=True)
            (root / "src" / "demo" / "__init__.py").write_text(
                "__all__ = []\n", encoding="utf-8"
            )
            _commit_all(root)

            discovered = discover_components(root)

            self.assertEqual(len(discovered), 1, discovered)
            component = next(iter(discovered.values()))
            self.assertEqual(component["path"], "src/demo")
            self.assertEqual(component["boundary"]["provider"], "python-exports")
            self.assertIsNone(component["version_source"])
            config = {
                "project": "demo",
                "components": discovered,
                "slices": {},
            }
            self.assertEqual(validate_config(config, root), [])

    def test_discovery_uses_only_tracked_files_for_provider_detection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            component = root / "svc"
            component.mkdir()
            (component / "package.json").write_text(
                '{"name":"svc","version":"1.0.0"}\n', encoding="utf-8"
            )
            (component / "__init__.py").write_text("__all__ = []\n", encoding="utf-8")
            _commit_all(root)
            (component / "openapi.yaml").write_text(
                "openapi: 3.1.0\npaths: {}\n", encoding="utf-8"
            )

            discovered = discover_components(root)

            self.assertEqual(len(discovered), 1, discovered)
            selected = next(iter(discovered.values()))
            self.assertEqual(selected["boundary"]["provider"], "python-exports")
            self.assertEqual(selected["boundary"]["paths"], ["__init__.py"])

    def test_discovery_ignores_tracked_provider_files_deleted_from_worktree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            component = root / "svc"
            component.mkdir()
            (component / "package.json").write_text(
                '{"name":"svc","version":"1.0.0"}\n', encoding="utf-8"
            )
            (component / "__init__.py").write_text(
                "__all__ = []\n", encoding="utf-8"
            )
            openapi = component / "openapi.yaml"
            openapi.write_text("openapi: 3.1.0\npaths: {}\n", encoding="utf-8")
            _commit_all(root)
            openapi.unlink()

            discovered = discover_components(root)

            selected = next(iter(discovered.values()))
            self.assertEqual(selected["boundary"]["provider"], "python-exports")

    def test_init_discover_does_not_write_an_invalid_empty_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "root-only"\nversion = "1.0.0"\n',
                encoding="utf-8",
            )
            _commit_all(root)

            exit_code, _stdout, stderr = _run_cli(
                "init", "--discover", repo_root=root
            )

            self.assertEqual(exit_code, core.EXIT_USAGE, stderr)
            self.assertFalse((root / "boundary.config.json").exists())
            self.assertIn("No tracked component could be discovered", stderr)


class SeverityIntegrityTests(unittest.TestCase):
    def test_component_name_cannot_spoof_a_compat_exit_code(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            component_name = "looks.compat:dangerous"
            (root / "svc").mkdir()
            (root / "svc" / "main.txt").write_text("before\n", encoding="utf-8")
            config = {
                "project": "p",
                "components": {
                    component_name: {
                        "path": "svc",
                        "boundary": {"provider": "implicit"},
                    }
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
            _commit_all(root)
            lockfile = core.generate_lockfile(
                config, root, source="working-tree", strict=True
            )
            (root / "boundary.lock.json").write_text(
                json.dumps(lockfile, indent=2) + "\n", encoding="utf-8"
            )
            (root / "svc" / "main.txt").write_text("after\n", encoding="utf-8")

            exit_code, _stdout, stderr = _run_cli(
                "verify",
                "--source",
                "working-tree",
                "--facets",
                "exact",
                "--format",
                "json",
                repo_root=root,
            )
            self.assertEqual(exit_code, core.EXIT_DRIFT, stderr)

    def test_fail_fast_severity_is_based_on_the_facet_not_component_names(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "spoof").mkdir()
            (root / "spoof" / "main.txt").write_text("before\n", encoding="utf-8")
            (root / "spoof" / "api.json").write_text("{}\n", encoding="utf-8")
            (root / "real").mkdir()
            (root / "real" / "api.json").write_text("{}\n", encoding="utf-8")
            config = {
                "project": "p",
                "components": {
                    "spoof.compat:signal": {
                        "path": "spoof",
                        "boundary": {
                            "provider": "json-file",
                            "paths": ["api.json"],
                        },
                    },
                    "real": {
                        "path": "real",
                        "boundary": {
                            "provider": "json-file",
                            "paths": ["api.json"],
                        },
                    },
                },
                "slices": {},
            }
            _commit_all(root)
            lockfile = core.generate_lockfile(
                config, root, source="working-tree", strict=True
            )
            (root / "spoof" / "main.txt").write_text("after\n", encoding="utf-8")
            (root / "real" / "api.json").write_text(
                '{"changed":true}\n', encoding="utf-8"
            )

            issues = core.verify_lockfile(
                config,
                lockfile,
                root,
                source="working-tree",
                facets=["exact", "boundary"],
                fail_fast=True,
            )
            self.assertEqual(len(issues), 1, issues)
            self.assertIn("real.boundary:", issues[0])


class PublicApiPolicyTests(unittest.TestCase):
    def test_diff_rejects_malformed_lockfiles_like_the_cli(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            malformed = root / "malformed.json"
            valid_shape = root / "valid-shape.json"
            malformed.write_text("[]\n", encoding="utf-8")
            valid_shape.write_text(
                json.dumps(
                    {
                        "schema": "boundary-lock/v3",
                        "config_contract": "boundver-semantic-config/v2",
                        "config_digest": "0" * 64,
                        "project": "p",
                        "components": {},
                        "slices": {},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(core.LockfileError, "root must be an object"):
                boundver.diff(str(malformed), str(valid_shape))

    def test_verify_rejects_configured_compat_gate_without_version_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.json").write_text("{}\n", encoding="utf-8")
            config = {
                "project": "public-api",
                "defaults": {"verify_facets": ["compat"]},
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {
                            "provider": "json-file",
                            "paths": ["api.json"],
                        },
                    }
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
            _commit_all(root)

            with patch("boundver._git.git_root", return_value=root):
                with self.assertRaisesRegex(core.ConfigError, "compat.*version_source"):
                    boundver.generate()

    def test_verify_checks_global_component_and_slice_sets_when_filtered(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_git_repo(root)
            for name in ("a", "b"):
                (root / name).mkdir()
                (root / name / "main.py").write_text(
                    f"# {name}\n", encoding="utf-8"
                )
            config = {
                "project": "public-api",
                "components": {
                    name: {
                        "path": name,
                        "boundary": {"provider": "implicit"},
                    }
                    for name in ("a", "b")
                },
                "slices": {
                    "all": {
                        "mode": "exact",
                        "components": ["a", "b"],
                    }
                },
            }
            (root / "boundary.config.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
            _commit_all(root)

            with patch("boundver._git.git_root", return_value=root):
                lockfile = boundver.generate(
                    source="working-tree", out_path=None
                )
                lockfile["components"].pop("b")
                lockfile["slices"].pop("all")
                (root / "boundary.lock.json").write_text(
                    json.dumps(lockfile), encoding="utf-8"
                )

                issues = boundver.verify(
                    source="working-tree", components=["a"]
                )

            self.assertTrue(
                any("component set differs" in issue for issue in issues),
                issues,
            )
            self.assertTrue(
                any("slice set differs" in issue for issue in issues),
                issues,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
