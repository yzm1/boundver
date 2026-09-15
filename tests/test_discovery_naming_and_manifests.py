"""What discovery names things, and what it promises those names resolve to.

Discovery writes a declaration a team then lives with, so its choices have to
be reproducible from the repository alone. Two of them are not obvious. Which
directory gets the bare name when two share a basename is decided by an
iteration order, not by the filesystem, and a regression there would swap two
components' names without failing anything. And a manifest at the repository
root does not describe the root: it is remapped onto a component directory, and
which directory that is - and whether an inner manifest outranks it - decides
both the component's name and where its version comes from.

The version_source discovery emits is a promise to a later command. It says
"the version is in this file under this field", and generate believes it, so a
field that cannot resolve has to be a refusal rather than a null identity
behind a facet the config declares as available.

Covers OBL-CONFIG-032, OBL-CONFIG-049, OBL-CONFIG-050, OBL-CONFIG-052 and
OBL-CONFIG-053.
"""

from __future__ import annotations

import json
import unittest

from boundver._discovery import (
    MAX_DISCOVERY_DIFF_COMPONENTS,
    compare_discovery_to_config,
    discover_components,
)
from boundver._utils import ConfigError
from boundver.versions import extract_version

from tests._parity import run_cli
from tests._scenarios import Scenario

USAGE = 2

#: Manifest kinds in the precedence order discovery walks them.
MANIFESTS = ("package.json", "pyproject.toml", "Cargo.toml", "go.mod")

PACKAGE_JSON = json.dumps({"name": "p", "version": "1.2.3"}, indent=2) + "\n"
PYPROJECT = '[project]\nname = "p"\nversion = "4.5.6"\n'
CARGO = '[package]\nname = "p"\nversion = "7.8.9"\n'
GO_MOD = "module example.com/p\n\ngo 1.22\n"

MANIFEST_CONTENT = {
    "package.json": PACKAGE_JSON,
    "pyproject.toml": PYPROJECT,
    "Cargo.toml": CARGO,
    "go.mod": GO_MOD,
}

#: What each manifest kind declares, and where.
DECLARED = {
    "package.json": ({"file": "package.json", "field": "version"}, "1.2.3"),
    "pyproject.toml": ({"file": "pyproject.toml", "field": "project.version"}, "4.5.6"),
    "Cargo.toml": ({"file": "Cargo.toml", "field": "package.version"}, "7.8.9"),
}


def _empty() -> Scenario:
    """A repository with no declared components, for discovery to fill."""
    scene = Scenario()
    scene.config["components"] = {}
    return scene


def _commit(scene: Scenario) -> Scenario:
    scene.git("add", "--all")
    scene.git("commit", "-m", "discovery fixture")
    return scene


class CollisionOrderTests(unittest.TestCase):
    """OBL-CONFIG-032: which directory gets the bare name is decided."""

    def test_the_lower_path_takes_the_bare_name(self):
        """Sorted path within one manifest kind, not filesystem order."""
        with _empty() as scene:
            for parent in ("zeta", "alpha"):
                scene.file(f"{parent}/svc/package.json", PACKAGE_JSON)
                scene.file(f"{parent}/svc/index.js", "x\n")
            _commit(scene)
            found = discover_components(scene.root)
            self.assertEqual(found["svc"]["path"], "alpha/svc")
            self.assertEqual(found["svc-2"]["path"], "zeta/svc")

    def test_the_manifest_kind_outranks_the_path(self):
        """A package.json anywhere is claimed before any pyproject.toml."""
        with _empty() as scene:
            scene.file("zeta/svc/package.json", PACKAGE_JSON)
            scene.file("zeta/svc/index.js", "x\n")
            scene.file("alpha/svc/pyproject.toml", PYPROJECT)
            scene.file("alpha/svc/main.py", "x\n")
            _commit(scene)
            found = discover_components(scene.root)
            self.assertEqual(found["svc"]["path"], "zeta/svc")
            self.assertEqual(found["svc-2"]["path"], "alpha/svc")

    def test_a_third_collision_continues_the_sequence(self):
        with _empty() as scene:
            for parent in ("c", "a", "b"):
                scene.file(f"{parent}/svc/package.json", PACKAGE_JSON)
                scene.file(f"{parent}/svc/index.js", "x\n")
            _commit(scene)
            found = discover_components(scene.root)
            self.assertEqual(
                [found[name]["path"] for name in ("svc", "svc-2", "svc-3")],
                ["a/svc", "b/svc", "c/svc"],
            )

    def test_an_unaddressable_directory_name_is_refused(self):
        """A comma would break the comma-separated component filters."""
        with _empty() as scene:
            scene.file("a,b/package.json", PACKAGE_JSON)
            scene.file("a,b/index.js", "x\n")
            _commit(scene)
            with self.assertRaises(ConfigError) as raised:
                discover_components(scene.root)
            self.assertIn("'a,b'", str(raised.exception))
            self.assertIn("is not addressable", str(raised.exception))
            self.assertIn("Rename the directory", str(raised.exception))

    def test_the_cli_maps_that_refusal_to_a_usage_exit(self):
        with _empty() as scene:
            scene.file("a,b/package.json", PACKAGE_JSON)
            scene.file("a,b/index.js", "x\n")
            _commit(scene)
            result = run_cli(scene.root, "discover", "--format", "json")
            self.assertEqual(result.returncode, USAGE)
            self.assertIn("not addressable", result.stderr)


class RootManifestRemapTests(unittest.TestCase):
    """OBL-CONFIG-049 and OBL-CONFIG-050: a root manifest describes what?"""

    def test_it_remaps_onto_the_single_top_level_python_package(self):
        with _empty() as scene:
            scene.file("package.json", PACKAGE_JSON)
            scene.file("pkg/__init__.py", "\n")
            scene.file("pkg/main.py", "x\n")
            _commit(scene)
            found = discover_components(scene.root)
            self.assertEqual(list(found), [scene.root.name])
            entry = found[scene.root.name]
            self.assertEqual(entry["path"], "pkg")
            self.assertIsNone(entry["version_source"])

    def test_it_falls_back_to_src_lib_app_in_that_order(self):
        """The clause with no package directory to remap onto."""
        for present, expected in (
            (("src", "lib", "app"), "src"),
            (("lib", "app"), "lib"),
            (("app",), "app"),
        ):
            with self.subTest(directories=present):
                with _empty() as scene:
                    scene.file("package.json", PACKAGE_JSON)
                    for name in present:
                        scene.file(f"{name}/main.js", "x\n")
                    _commit(scene)
                    found = discover_components(scene.root)
                    self.assertEqual(
                        found[scene.root.name]["path"], expected
                    )

    def test_a_root_manifest_with_nowhere_to_go_is_skipped(self):
        with _empty() as scene:
            scene.file("package.json", PACKAGE_JSON)
            scene.file("notes/readme.md", "x\n")
            _commit(scene)
            self.assertEqual(discover_components(scene.root), {})

    def test_the_name_is_the_repository_not_the_target_directory(self):
        with _empty() as scene:
            scene.file("package.json", PACKAGE_JSON)
            scene.file("src/main.js", "x\n")
            _commit(scene)
            found = discover_components(scene.root)
            self.assertEqual(list(found), [scene.root.name])
            self.assertNotIn("src", found)

    def test_the_version_source_is_null_even_though_the_manifest_declares_one(self):
        for manifest in ("package.json", "pyproject.toml", "Cargo.toml"):
            with self.subTest(manifest=manifest):
                with _empty() as scene:
                    scene.file(manifest, MANIFEST_CONTENT[manifest])
                    scene.file("src/main.py", "x\n")
                    _commit(scene)
                    found = discover_components(scene.root)
                    self.assertIsNone(
                        found[scene.root.name]["version_source"], manifest
                    )

    def test_boundary_evidence_comes_only_from_under_the_target(self):
        with _empty() as scene:
            scene.file("package.json", PACKAGE_JSON)
            scene.file("openapi.yaml", "openapi: 3.1.0\n")
            scene.file("src/__init__.py", "\n")
            scene.file("src/main.py", "x\n")
            _commit(scene)
            entry = discover_components(scene.root)[scene.root.name]
            self.assertEqual(entry["path"], "src")
            for path in entry["boundary"]["paths"]:
                self.assertNotEqual(path, "openapi.yaml")
                self.assertNotIn("..", path)

    def test_the_target_is_claimed_once(self):
        """A second root manifest of a lower kind adds no second component."""
        with _empty() as scene:
            scene.file("package.json", PACKAGE_JSON)
            scene.file("pyproject.toml", PYPROJECT)
            scene.file("src/main.py", "x\n")
            _commit(scene)
            found = discover_components(scene.root)
            self.assertEqual(list(found), [scene.root.name])

    def _shadowing(self, scene, directory: str, manifest: str) -> dict:
        """A root package.json over a directory carrying its own manifest."""
        scene.file("package.json", PACKAGE_JSON)
        if directory.count("/"):
            scene.file(f"{directory}/__init__.py", "\n")
        scene.file(f"{directory}/{manifest}", MANIFEST_CONTENT[manifest])
        scene.file(f"{directory}/index.js", "x\n")
        _commit(scene)
        return discover_components(scene.root)

    def test_an_inner_manifest_owns_its_own_directory(self):
        """OBL-CONFIG-050: the root manifest must not claim it."""
        for directory in ("app", "lib"):
            with self.subTest(directory=directory):
                with _empty() as scene:
                    found = self._shadowing(scene, directory, "package.json")
                    self.assertEqual(list(found), [directory])
                    self.assertEqual(found[directory]["path"], directory)
                    self.assertEqual(
                        found[directory]["version_source"],
                        {"file": "package.json", "field": "version"},
                    )

    def test_it_owns_it_under_src_as_well(self):
        """A nested manifest owns its source directory before a root remap."""
        for directory, manifest in (
            ("src", "package.json"),
            ("src/pkg", "package.json"),
            ("src/pkg", "pyproject.toml"),
        ):
            with self.subTest(directory=directory, manifest=manifest):
                with _empty() as scene:
                    found = self._shadowing(scene, directory, manifest)
                    self.assertNotIn(scene.root.name, found)

    def test_the_nested_owner_keeps_its_name_and_version_source(self):
        """Pin every consequence, for each route into the prior defect."""
        for directory, manifest in (
            ("src", "package.json"),
            ("src/pkg", "package.json"),
            ("src/pkg", "pyproject.toml"),
        ):
            with self.subTest(directory=directory, manifest=manifest):
                with _empty() as scene:
                    found = self._shadowing(scene, directory, manifest)
                    self.assertEqual(list(found), [directory.rsplit("/", 1)[-1]])
                    entry = found[directory.rsplit("/", 1)[-1]]
                    self.assertEqual(entry["path"], directory)
                    self.assertEqual(
                        entry["version_source"],
                        DECLARED.get(manifest, (None, None))[0],
                    )

    def test_inner_manifest_ownership_is_not_decided_by_directory_sort_order(self):
        """Equivalent app and src layouts give their nested manifest ownership."""
        outcomes = {}
        for directory in ("app", "src"):
            with _empty() as scene:
                found = self._shadowing(scene, directory, "package.json")
                outcomes[directory] = (
                    "the directory" if directory in found else "the repository"
                )
        self.assertEqual(outcomes, {"app": "the directory", "src": "the directory"})


class VersionSourceRoundTripTests(unittest.TestCase):
    """OBL-CONFIG-053: what discovery promises, extract_version must deliver."""

    def test_every_emitted_source_resolves_to_the_declared_version(self):
        for manifest, (expected_source, expected_version) in DECLARED.items():
            with self.subTest(manifest=manifest):
                with _empty() as scene:
                    scene.file(f"svc/{manifest}", MANIFEST_CONTENT[manifest])
                    scene.file("svc/main.py", "x\n")
                    _commit(scene)
                    entry = discover_components(scene.root)["svc"]
                    self.assertEqual(entry["version_source"], expected_source)
                    self.assertEqual(
                        extract_version(scene.root, "svc", entry["version_source"]),
                        expected_version,
                    )

    def test_go_mod_emits_no_version_source_at_all(self):
        """Because the field reader dispatches on suffix and .mod is not one."""
        with _empty() as scene:
            scene.file("svc/go.mod", GO_MOD)
            scene.file("svc/main.go", "package main\n")
            _commit(scene)
            entry = discover_components(scene.root)["svc"]
            self.assertIsNone(entry["version_source"])

    def test_a_field_that_cannot_resolve_is_a_refusal_not_a_null(self):
        """A Poetry-only pyproject and a workspace-inherited Cargo version."""
        cases = {
            "poetry only": (
                "pyproject.toml",
                '[tool.poetry]\nname = "p"\nversion = "1.0.0"\n',
                {"file": "pyproject.toml", "field": "project.version"},
            ),
            "workspace cargo": (
                "Cargo.toml",
                '[package]\nname = "p"\nversion.workspace = true\n',
                {"file": "Cargo.toml", "field": "package.version"},
            ),
        }
        for label, (manifest, content, source) in cases.items():
            with self.subTest(layout=label):
                with _empty() as scene:
                    scene.config["components"] = {
                        "svc": {
                            "path": "svc",
                            "version_source": source,
                            "boundary": {"provider": "leaf", "paths": []},
                        }
                    }
                    scene.file(f"svc/{manifest}", content)
                    scene.file("svc/main.py", "x\n")
                    scene.commit()
                    result = run_cli(scene.root, "generate", "--source", "head")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(
                        "Configured version source did not produce a version",
                        result.stdout + result.stderr,
                    )

    def test_the_resolvable_layout_generates_cleanly(self):
        """The premise: the refusal above is about the field, not the fixture."""
        with _empty() as scene:
            scene.config["components"] = {
                "svc": {
                    "path": "svc",
                    "version_source": {
                        "file": "pyproject.toml", "field": "project.version"
                    },
                    "boundary": {"provider": "leaf", "paths": []},
                }
            }
            scene.file("svc/pyproject.toml", PYPROJECT)
            scene.file("svc/main.py", "x\n")
            scene.commit()
            self.assertEqual(
                run_cli(scene.root, "generate", "--source", "head").returncode, 0
            )
            lock = json.loads(
                (scene.root / "boundary.lock.json").read_text(encoding="utf-8")
            )
            self.assertEqual(lock["components"]["svc"]["version"], "4.5.6")


class DiffPathMatchingTests(unittest.TestCase):
    """OBL-CONFIG-052: both sides are normalized, and nothing else is."""

    DISCOVERED = {"svc": {"path": "src/a", "boundary": {"provider": "leaf", "paths": []}}}

    def _config(self, path: str) -> dict:
        return {"components": {"svc": {"path": path}}}

    def test_a_trailing_slash_still_matches(self):
        report = compare_discovery_to_config(self.DISCOVERED, self._config("src/a/"))
        self.assertEqual(report["registered_count"], 1)
        self.assertEqual(report["unregistered_count"], 0)
        self.assertEqual(report["not_discovered_count"], 0)

    def test_an_exact_spelling_matches(self):
        """The premise: this pair is a match before any normalization."""
        report = compare_discovery_to_config(self.DISCOVERED, self._config("src/a"))
        self.assertEqual(report["registered_count"], 1)

    def test_matching_stays_case_sensitive_on_every_host(self):
        report = compare_discovery_to_config(self.DISCOVERED, self._config("Src/a"))
        self.assertEqual(report["registered_count"], 0)
        self.assertEqual(report["unregistered"], [{"name": "svc", "path": "src/a"}])
        self.assertEqual(report["not_discovered"], [{"name": "svc", "path": "Src/a"}])

    def test_a_rejected_configured_path_raises_rather_than_disappearing(self):
        for path, reason in (
            ("src" + chr(92) + "a", "separators"),
            ("/src/a", "relative"),
            ("src/../a", ".."),
            ("  src/a  ", "whitespace"),
        ):
            with self.subTest(path=path):
                with self.assertRaises(ConfigError) as raised:
                    compare_discovery_to_config(self.DISCOVERED, self._config(path))
                self.assertIn("svc", str(raised.exception))

    def test_the_component_ceiling_refuses_rather_than_truncates(self):
        def config(count):
            return {
                "components": {
                    f"c{index:05d}": {"path": f"src/{index:05d}"}
                    for index in range(count)
                }
            }

        accepted = compare_discovery_to_config({}, config(MAX_DISCOVERY_DIFF_COMPONENTS))
        self.assertEqual(
            accepted["not_discovered_count"], MAX_DISCOVERY_DIFF_COMPONENTS
        )
        with self.assertRaises(ConfigError) as raised:
            compare_discovery_to_config({}, config(MAX_DISCOVERY_DIFF_COMPONENTS + 1))
        self.assertIn(f"{MAX_DISCOVERY_DIFF_COMPONENTS}-component limit", str(raised.exception))

    def test_an_over_long_name_on_either_side_is_refused(self):
        long_name = "c" * (16_384 + 1)
        with self.assertRaises(ConfigError) as raised:
            compare_discovery_to_config(
                self.DISCOVERED, {"components": {long_name: {"path": "src/a"}}}
            )
        self.assertIn("Configured component name", str(raised.exception))
        with self.assertRaises(ConfigError) as raised:
            compare_discovery_to_config(
                {long_name: {"path": "src/a"}}, self._config("src/a")
            )
        self.assertIn("Discovered component name", str(raised.exception))

    def test_the_cli_maps_a_refusal_to_a_usage_exit(self):
        with _empty() as scene:
            scene.config["components"] = {"svc": {"path": "/absolute"}}
            scene.file("svc/main.py", "x\n")
            scene.commit()
            result = run_cli(scene.root, "discover", "--diff-config")
            self.assertEqual(result.returncode, USAGE)


if __name__ == "__main__":
    unittest.main()
