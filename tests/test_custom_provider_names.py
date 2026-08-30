"""Regression tests for explicit and runtime-resolved custom-provider names."""

import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

from boundver._config import validate_config
from boundver._lockfile import generate_lockfile
from boundver.providers import ResolvedBoundary


class ExplicitProvider:
    name = "custom.explicit"
    version = "1"

    def resolve(self, ctx):
        return ResolvedBoundary(entries=[("contract", b"stable")])


class ImplicitProvider:
    name = "custom.implicit"
    version = "1"

    def resolve(self, ctx):
        return ResolvedBoundary(entries=[("contract", b"stable")])


class CustomProviderNameTests(unittest.TestCase):
    module_name = "_boundver_custom_provider_name_fixture"

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=self.root,
            check=True,
        )
        component = self.root / "svc"
        component.mkdir()
        (component / "contract.txt").write_text("contract\n", encoding="utf-8")
        subprocess.run(["git", "add", "svc/contract.txt"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "fixture"],
            cwd=self.root,
            check=True,
        )

        module = types.ModuleType(self.module_name)
        module.ExplicitProvider = ExplicitProvider
        module.ImplicitProvider = ImplicitProvider
        sys.modules[self.module_name] = module

    def tearDown(self):
        sys.modules.pop(self.module_name, None)
        self.temporary_directory.cleanup()

    def _entry(self, provider_class, *, explicit_name):
        entry = {"module": self.module_name, "class": provider_class.__name__}
        if explicit_name:
            entry["name"] = provider_class.name
        return entry

    def _config(self, providers, *, selected_name="custom.implicit"):
        return {
            "project": "custom-provider-names",
            "providers": providers,
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": selected_name},
                }
            },
            "slices": {},
        }

    def test_explicit_implicit_and_mixed_lists_validate_and_generate(self):
        declarations = {
            "all explicit": [
                self._entry(ExplicitProvider, explicit_name=True),
                self._entry(ImplicitProvider, explicit_name=True),
            ],
            "all runtime-resolved": [
                self._entry(ExplicitProvider, explicit_name=False),
                self._entry(ImplicitProvider, explicit_name=False),
            ],
            "explicit then runtime-resolved": [
                self._entry(ExplicitProvider, explicit_name=True),
                self._entry(ImplicitProvider, explicit_name=False),
            ],
            "runtime-resolved then explicit": [
                self._entry(ImplicitProvider, explicit_name=False),
                self._entry(ExplicitProvider, explicit_name=True),
            ],
        }
        boundary_digests = set()

        for label, providers in declarations.items():
            with self.subTest(label=label):
                config = self._config(providers)
                self.assertEqual(
                    validate_config(
                        config,
                        self.root,
                        source="head",
                        allow_custom_providers=True,
                    ),
                    [],
                )
                lockfile = generate_lockfile(
                    config,
                    self.root,
                    source="head",
                    strict=True,
                    allow_custom_providers=True,
                )
                component = lockfile["components"]["svc"]
                self.assertEqual(component["boundary_provider"], "custom.implicit")
                self.assertEqual(component["boundary_provider_version"], "1")
                self.assertEqual(component["boundary_status"], "ok")
                boundary_digests.add(component["fingerprints"]["boundary"])

        self.assertEqual(len(boundary_digests), 1)

    def test_mixed_list_is_not_falsely_name_checked_without_trusted_loading(self):
        config = self._config(
            [
                self._entry(ExplicitProvider, explicit_name=True),
                self._entry(ImplicitProvider, explicit_name=False),
            ]
        )

        errors = validate_config(config, self.root, source="head")

        self.assertFalse(
            any("not declared in the 'providers' list" in error for error in errors),
            errors,
        )

    def test_missing_runtime_provider_fails_after_trusted_loading(self):
        config = self._config(
            [
                self._entry(ExplicitProvider, explicit_name=True),
                self._entry(ImplicitProvider, explicit_name=False),
            ],
            selected_name="custom.missing",
        )

        errors = validate_config(
            config,
            self.root,
            source="head",
            allow_custom_providers=True,
        )

        self.assertTrue(
            any(
                "provider 'custom.missing' was not registered" in error
                for error in errors
            ),
            errors,
        )

    def test_mismatched_explicit_and_runtime_names_fail_closed(self):
        declaration = self._entry(ExplicitProvider, explicit_name=True)
        declaration["name"] = "custom.configured"
        config = self._config(
            [declaration],
            selected_name="custom.configured",
        )

        errors = validate_config(
            config,
            self.root,
            source="head",
            allow_custom_providers=True,
        )

        mismatch = [
            error
            for error in errors
            if "does not match configured name='custom.configured'" in error
        ]
        self.assertEqual(len(mismatch), 1, errors)
        self.assertLess(len(mismatch[0]), 1024)

    def test_empty_provider_list_is_conclusively_missing_reference(self):
        config = self._config([], selected_name="custom.missing")

        errors = validate_config(config, self.root, source="head")

        self.assertTrue(
            any("not declared in the 'providers' list" in error for error in errors),
            errors,
        )


if __name__ == "__main__":
    unittest.main()
