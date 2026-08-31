"""Vacuous explicit slices must fail closed on every public path."""

from __future__ import annotations

import builtins
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import boundver._config as config_module
from boundver._hashing import canonical_json, sha256_hex
from boundver._lockfile import generate_lockfile, verify_lockfile
from boundver._utils import ConfigError
from tests._repo_fixtures import commit_all, init_git_repo


REPO_ROOT = Path(__file__).parents[1]
EMPTY_SLICE_GUIDANCE = "add a component name or remove the empty slice"


def _component() -> dict:
    return {
        "path": "svc",
        "version_source": {"file": "version.json", "field": "version"},
        "boundary": {"provider": "implicit", "paths": ["contract.json"]},
        "behavior": {"paths": ["contract.json"]},
    }


def _config_with_slice(mode: str, members: list[str]) -> dict:
    return {
        "project": "empty-slice-test",
        "components": {"svc": _component()},
        "slices": {
            "release-gate": {
                "mode": mode,
                "components": members,
            }
        },
    }


class EmptySliceSchemaAndValidationTests(unittest.TestCase):
    def test_public_and_packaged_schemas_require_one_explicit_member(self) -> None:
        try:
            import jsonschema
        except ImportError:  # pragma: no cover - optional dependency
            self.skipTest("jsonschema is not installed")

        root_schema = json.loads(
            (REPO_ROOT / "boundary.config.schema.json").read_text(encoding="utf-8")
        )
        packaged_schema = json.loads(
            (REPO_ROOT / "src" / "boundver" / "boundary.config.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(root_schema, packaged_schema)

        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(root_schema).validate(
                _config_with_slice("exact", [])
            )
        jsonschema.Draft202012Validator(root_schema).validate(
            _config_with_slice("exact", ["svc"])
        )

    def test_dependency_free_validation_rejects_empty_slice_actionably(self) -> None:
        real_import = builtins.__import__

        def import_without_jsonschema(name, *args, **kwargs):
            if name == "jsonschema" or name.startswith("jsonschema."):
                raise ImportError("jsonschema deliberately unavailable")
            return real_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            with patch("builtins.__import__", side_effect=import_without_jsonschema):
                errors = config_module.validate_config(
                    _config_with_slice("exact", []),
                    root,
                )

        self.assertTrue(any(EMPTY_SLICE_GUIDANCE in error for error in errors), errors)


class EmptySliceGenerationAndVerificationTests(unittest.TestCase):
    def test_generate_rejects_every_empty_slice_mode_even_when_partial(self) -> None:
        for mode in ("exact", "behavior", "boundary", "compat"):
            for strict in (True, False):
                with self.subTest(mode=mode, strict=strict):
                    with self.assertRaises(ConfigError) as raised:
                        generate_lockfile(
                            _config_with_slice(mode, []),
                            Path.cwd(),
                            source="working-tree",
                            strict=strict,
                        )
                    self.assertIn(EMPTY_SLICE_GUIDANCE, str(raised.exception))

    def test_verify_rejects_every_empty_slice_mode_before_drift(self) -> None:
        for mode in ("exact", "behavior", "boundary", "compat"):
            with self.subTest(mode=mode):
                issues = verify_lockfile(
                    _config_with_slice(mode, []),
                    {},
                    Path.cwd(),
                    source="working-tree",
                )
                self.assertEqual(len(issues), 1)
                self.assertIn(EMPTY_SLICE_GUIDANCE, issues[0])

    def test_nonempty_and_closure_slice_fingerprints_keep_the_same_formula(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            init_git_repo(root)
            component_root = root / "svc"
            component_root.mkdir()
            (component_root / "main.py").write_text("value = 1\n", encoding="utf-8")
            (component_root / "contract.json").write_text(
                '{"kind":"contract"}\n', encoding="utf-8"
            )
            (component_root / "version.json").write_text(
                '{"version":"1.2.3"}\n', encoding="utf-8"
            )
            commit_all(root, "fixture")
            config = {
                "project": "nonempty-slice-test",
                "components": {"svc": _component()},
                "slices": {
                    **{
                        f"explicit-{mode}": {
                            "mode": mode,
                            "components": ["svc"],
                        }
                        for mode in ("exact", "behavior", "boundary", "compat")
                    },
                    "closure-exact": {"mode": "exact", "closure_of": "svc"},
                },
            }

            lockfile = generate_lockfile(config, root, source="working-tree")

        component_fingerprints = lockfile["components"]["svc"]["fingerprints"]
        for mode in ("exact", "behavior", "boundary", "compat"):
            with self.subTest(mode=mode):
                expected = sha256_hex(
                    canonical_json({"svc": component_fingerprints[mode]})
                )
                self.assertEqual(
                    lockfile["slices"][f"explicit-{mode}"]["fingerprint"],
                    expected,
                )
                self.assertEqual(
                    lockfile["slices"][f"explicit-{mode}"]["components"],
                    ["svc"],
                )
        self.assertEqual(
            lockfile["slices"]["closure-exact"]["fingerprint"],
            lockfile["slices"]["explicit-exact"]["fingerprint"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
