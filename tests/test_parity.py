"""Differential tests where boundver is its own oracle.

The register's largest differential group is not boundver against something
else, it is boundver against boundver under a different spelling: two source
modes on a clean tree, a raw and a canonical provider resolving the same
declaration, one config written three ways, a flag and its default. Each names
an axis that must not change the answer, so the test is the same each time and
only the axis differs.

Covers OBL-GIT-SOURCE-059, OBL-GIT-SOURCE-070, OBL-GLOBS-010 and
OBL-HASHING-050.
"""

from __future__ import annotations

import json
import unittest

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from boundver._config import load_config_file
from boundver._lockfile import generate_lockfile
from boundver._utils import ConfigError

from tests._parity import (
    assert_source_modes_agree,
    assert_variants_agree,
    describe,
    partition,
    run_cli,
)
from tests._scenarios import SOURCE_MODES, Scenario, requires_symlinks

PROFILE = settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow],
)

NUL = bytes([0])


def _varied_tree(scene: Scenario) -> Scenario:
    """One component holding every file shape the source modes must agree on."""
    scene.component("svc", path="svc", boundary=["api"], behavior=["api", "cfg.json"])
    scene.file("svc/api/lf.yaml", "openapi: 3.1.0\nkey: value\n")
    scene.file("svc/api/crlf.yaml", "openapi: 3.1.0\nkey: value\n", crlf=True)
    scene.file("svc/api/nested/deep/spec.yaml", "openapi: 3.1.0\n")
    scene.file("svc/api/.hidden.yaml", "openapi: 3.1.0\n")
    scene.file("svc/api/unicode-é中.yaml", "openapi: 3.1.0\n")
    scene.json_file("svc/cfg.json", {"retries": 3, "nested": {"a": [1, 2]}})
    scene.file("svc/run.sh", "#!/bin/sh\necho hi\n", executable=True)
    (scene.root / "svc" / "api" / "binary.bin").write_bytes(
        b"header" + NUL + b"body" + NUL + bytes(range(32))
    )
    scene.file("svc/api/lone-cr.yaml", "a\rb\rc\n")
    scene.file("svc/api/no-trailing-newline.yaml", "openapi: 3.1.0")
    scene.file("svc/api/empty.yaml", "")
    return scene


class SourceModeParityTests(unittest.TestCase):
    """OBL-GIT-SOURCE-059 and OBL-GIT-SOURCE-070.

    On a clean tree, head, index and working-tree are three views of one state,
    so every facet must come out byte-identical. No lockfile field records
    which mode produced it, so the comparison is the whole document.
    """

    def test_every_source_mode_agrees_on_a_varied_clean_tree(self):
        with Scenario() as scene:
            _varied_tree(scene).commit()
            assert_source_modes_agree(self, scene, "varied clean tree")

    @requires_symlinks
    def test_source_modes_agree_when_the_tree_holds_a_symlink(self):
        with Scenario() as scene:
            _varied_tree(scene)
            scene.symlink("svc/api/link.yaml", "lf.yaml")
            scene.commit()
            assert_source_modes_agree(self, scene, "tree with a symlink")

    def test_source_modes_agree_when_the_tree_holds_a_submodule_gitlink(self):
        """The gitlink sits outside the component, and reaches HEAD.

        `commit` stages the working tree first, which deletes an index entry
        with nothing on disk, so this uses `commit_index`. The assertion that
        the entry is in HEAD is not decoration: without it this test passed
        against a tree that never held the gitlink at all.
        """
        with Scenario() as scene:
            _varied_tree(scene).commit()
            scene.submodule("vendor/sub")
            scene.commit_index("add submodule")
            self.assertIn("vendor/sub", scene.git("ls-tree", "-r", "HEAD"))
            assert_source_modes_agree(self, scene, "tree with a gitlink")

    def test_a_gitlink_inside_a_component_is_refused_by_every_source_mode(self):
        """A submodule under a component path cannot be hashed, in any mode.

        What must agree is the refusal, not its wording. head and index read an
        index entry and say "commit mode 160000"; working-tree reads a
        directory and says "unsupported working-tree file type". The readers
        genuinely see different things, so requiring one sentence would pin an
        accident. Requiring that all three fail closed and name the path is the
        actual obligation.
        """
        refusals = {}
        for mode in SOURCE_MODES:
            with Scenario() as scene:
                _varied_tree(scene).commit()
                scene.submodule("svc/vendor/sub")
                scene.commit_index("add submodule")
                with self.assertRaises(ConfigError) as caught:
                    scene.generate(source=mode)
                refusals[mode] = str(caught.exception)
        for mode, message in refusals.items():
            with self.subTest(mode=mode):
                self.assertIn("svc/vendor/sub", message)
                self.assertIn("Exact digest failed", message)
        self.assertIn("commit mode 160000", refusals["head"])
        self.assertEqual(refusals["head"], refusals["index"])

    def test_source_modes_agree_under_core_filemode_false(self):
        with Scenario() as scene:
            _varied_tree(scene).commit()
            scene.git("config", "core.filemode", "false")
            assert_source_modes_agree(self, scene, "core.filemode=false")

    def test_source_modes_agree_when_the_index_carries_the_executable_bit(self):
        """`core.filemode=false` is the Windows default, and the path it takes.

        With it set, `_working_tree_mode` returns the tracked entry's mode
        rather than reading the filesystem, so the executable bit survives on a
        host that cannot store one. This runs on every leg, unlike a chmod.
        """
        with Scenario() as scene:
            _varied_tree(scene).commit()
            scene.git("config", "core.filemode", "false")
            scene.git("update-index", "--chmod=+x", "svc/run.sh")
            scene.commit_index("mark executable")
            self.assertEqual(scene.git("ls-files", "--stage", "svc/run.sh").split()[0],
                             "100755")
            assert_source_modes_agree(self, scene, "executable bit set through the index")

    def test_a_mode_the_filesystem_cannot_store_leaves_the_tree_dirty(self):
        """Not a parity failure: Git reports the same modification.

        Under `core.filemode=true` on a filesystem with no execute bit, setting
        100755 through the index leaves the working tree genuinely different
        from what Git recorded. `git status` says so, and boundver's source
        modes differ for that reason rather than from a plumbing fault.
        """
        with Scenario() as scene:
            _varied_tree(scene).commit()
            scene.git("config", "core.filemode", "true")
            scene.git("update-index", "--chmod=+x", "svc/run.sh")
            scene.commit_index("mark executable")
            status = scene.git("status", "--porcelain")
            digests = {mode: scene.digest("svc", "exact", source=mode)
                       for mode in SOURCE_MODES}
            if status:
                self.assertIn("svc/run.sh", status)
                self.assertGreater(len(set(digests.values())), 1, digests)
            else:
                self.assertEqual(len(set(digests.values())), 1, digests)

    def test_source_modes_agree_across_several_components_and_a_slice(self):
        with Scenario() as scene:
            _varied_tree(scene)
            scene.component("sdk", path="sdk", provider="leaf")
            scene.component("app", path="app", provider="implicit")
            scene.slice("all", mode="exact", components=["svc", "sdk", "app"])
            scene.file("sdk/index.ts", "export const x = 1;\n")
            scene.file("app/main.py", "print(1)\n")
            scene.commit()
            assert_source_modes_agree(self, scene, "three components and a slice")

    def test_a_dirty_tree_separates_the_modes(self):
        """The parity assertion has to be capable of failing.

        Every test above would pass against a generator that returned a
        constant. This one shows the three modes really are read separately.
        """
        with Scenario() as scene:
            _varied_tree(scene).commit()
            scene.append_line("svc/api/lf.yaml", "staged: true")
            scene.stage()
            scene.file("svc/api/lf.yaml", "openapi: 3.1.0\nworking: only\n")
            results = {mode: scene.generate(source=mode) for mode in SOURCE_MODES}
            self.assertEqual(len(partition(results)), 3, describe(results))

    @PROFILE
    @given(
        names=st.lists(
            st.text(alphabet="abcXY.-_", min_size=1, max_size=6),
            min_size=1,
            max_size=5,
            unique=True,
        ),
        crlf=st.lists(st.booleans(), min_size=1, max_size=5),
        depth=st.integers(min_value=0, max_value=3),
    )
    def test_generated_clean_trees_agree_across_source_modes(self, names, crlf, depth):
        with Scenario() as scene:
            scene.component("svc", path="svc", boundary=["api"])
            prefix = "/".join(["d"] * depth)
            for index, name in enumerate(names):
                relative = f"svc/api/{prefix}/{name}.yaml" if prefix else f"svc/api/{name}.yaml"
                scene.file(
                    relative,
                    f"openapi: 3.1.0\nname: {name}\n",
                    crlf=crlf[index % len(crlf)],
                )
            scene.commit()
            assert_source_modes_agree(self, scene, f"generated tree {names}")


class ConfigFormatParityTests(unittest.TestCase):
    """OBL-HASHING-050: one logical config, three spellings, one digest."""

    JSON = json.dumps(
        {
            "project": "formats",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "path-hash", "paths": ["api"]},
                    "behavior": {"paths": ["api", "cfg.json"]},
                    "consumers": ["sdk"],
                },
                "sdk": {
                    "path": "sdk",
                    "boundary": {"provider": "leaf", "paths": []},
                },
            },
            "slices": {"all": {"mode": "exact", "components": ["sdk", "svc"]}},
        },
        indent=2,
    )

    YAML = """\
project: formats
components:
  svc:
    path: svc
    boundary:
      provider: path-hash
      paths:
        - api
    behavior:
      paths:
        - api
        - cfg.json
    consumers:
      - sdk
  sdk:
    path: sdk
    boundary:
      provider: leaf
      paths: []
slices:
  all:
    mode: exact
    components:
      - sdk
      - svc
"""

    TOML = """\
project = "formats"

[components.svc]
path = "svc"
consumers = ["sdk"]

[components.svc.boundary]
provider = "path-hash"
paths = ["api"]

[components.svc.behavior]
paths = ["api", "cfg.json"]

[components.sdk]
path = "sdk"

[components.sdk.boundary]
provider = "leaf"
paths = []

[slices.all]
mode = "exact"
components = ["sdk", "svc"]
"""

    def _repository(self) -> Scenario:
        scene = Scenario("formats")
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.json_file("svc/cfg.json", {"retries": 3})
        scene.file("sdk/index.ts", "export const x = 1;\n")
        for name, text in (
            ("boundary.config.json", self.JSON),
            ("boundary.config.yaml", self.YAML),
            ("boundary.config.toml", self.TOML),
        ):
            (scene.root / name).write_bytes(text.encode("utf-8"))
        scene.git("add", "--all")
        scene.git("commit", "--allow-empty", "-m", "formats")
        return scene

    def test_the_three_formats_parse_to_the_same_configuration(self):
        with self._repository() as scene:
            loaded = {
                suffix: load_config_file(scene.root / f"boundary.config.{suffix}")
                for suffix in ("json", "yaml", "toml")
            }
            self.assertEqual(len(partition(loaded)), 1, describe(loaded))

    def test_the_three_formats_produce_the_same_lockfile(self):
        with self._repository() as scene:
            locks = {
                suffix: generate_lockfile(
                    load_config_file(scene.root / f"boundary.config.{suffix}"),
                    scene.root,
                    source="head",
                )
                for suffix in ("json", "yaml", "toml")
            }
            self.assertEqual(len(partition(locks)), 1, describe(locks))
            digests = {key: lock["config_digest"] for key, lock in locks.items()}
            self.assertEqual(len(set(digests.values())), 1, digests)


class ProviderSelectionParityTests(unittest.TestCase):
    """OBL-GLOBS-010: raw and canonical providers select the same files."""

    def _build(self) -> Scenario:
        scene = Scenario("selection")
        scene.json_file("cfg/a.json", {"b": 2, "a": 1})
        scene.json_file("cfg/nested/b.json", {"x": [1, 2]})
        scene.json_file("cfg/nested/deep/c.json", {"y": None})
        scene.file("cfg/notes.md", "not selected\n")
        return scene

    def _pair(self, declaration):
        def variant(provider):
            def apply(scene: Scenario) -> None:
                scene.component("cfg", path="cfg", provider=provider,
                                boundary=list(declaration))
                scene.commit()

            return apply

        return [
            ("json-file", variant("json-file")),
            ("json-canonical", variant("json-canonical")),
        ]

    def _observe(self, scene: Scenario):
        """The outcome, whichever kind it is.

        A declaration matching nothing raises out of generate rather than
        recording an error status, and the two providers must agree on that
        too, so the refusal is part of what is compared.
        """
        try:
            entry = scene.generate()["components"]["cfg"]
        except ConfigError as error:
            return {"raised": type(error).__name__, "message": str(error)}
        return {
            "status": entry["boundary_status"],
            "errors": entry.get("boundary_errors", []),
        }

    def test_a_literal_declaration_resolves_alike(self):
        assert_variants_agree(self, self._build, self._pair(["a.json"]),
                              self._observe, "literal declaration")

    def test_a_single_star_declaration_resolves_alike(self):
        assert_variants_agree(self, self._build, self._pair(["*.json"]),
                              self._observe, "single-star declaration")

    def test_a_recursive_declaration_resolves_alike(self):
        assert_variants_agree(self, self._build, self._pair(["**/*.json"]),
                              self._observe, "recursive declaration")

    def test_a_directory_literal_resolves_alike(self):
        assert_variants_agree(self, self._build, self._pair(["nested"]),
                              self._observe, "directory literal")

    def _digest(self, provider, declaration, extra_files=()):
        with Scenario("selection") as scene:
            scene.json_file("cfg/a.json", {"b": 2, "a": 1})
            scene.json_file("cfg/nested/b.json", {"x": [1, 2]})
            for name in extra_files:
                scene.json_file(f"cfg/{name}", {"n": name})
            scene.component("cfg", path="cfg", provider=provider,
                            boundary=list(declaration))
            scene.commit()
            return scene.digest("cfg", "boundary")

    def test_declaring_the_same_file_twice_changes_nothing(self):
        """Both providers must de-duplicate, not just the raw one."""
        for provider in ("json-file", "json-canonical"):
            with self.subTest(provider=provider):
                once = self._digest(provider, ["a.json"])
                twice = self._digest(provider, ["a.json", "a.json"])
                overlapping = self._digest(provider, ["a.json", "*.json"])
                self.assertEqual(once, twice)
                self.assertEqual(once, overlapping)

    def test_declaration_order_does_not_reach_the_digest(self):
        """Selection is sorted, so the order two declarations were written in
        cannot survive into the result.

        The names are chosen so a case-insensitive or locale-aware sort would
        order them differently from their UTF-8 bytes: `B` is 0x42 and `a` is
        0x61.
        """
        files = ("B.json", "a.json", "Z.json", "é.json")
        for provider in ("json-file", "json-canonical"):
            with self.subTest(provider=provider):
                forward = self._digest(provider, list(files), extra_files=files)
                reverse = self._digest(
                    provider, list(reversed(files)), extra_files=files
                )
                self.assertEqual(forward, reverse)

    def test_a_declaration_matching_nothing_fails_alike(self):
        results = assert_variants_agree(
            self, self._build, self._pair(["missing/*.json"]),
            self._observe, "unmatched declaration",
        )
        self.assertIn("matched no tracked files", results["json-file"]["message"])


class OutputFormatParityTests(unittest.TestCase):
    """A view must not change a verdict: text and JSON share an exit code."""

    def _generated(self) -> Scenario:
        scene = Scenario("views")
        scene.component("svc", path="svc", boundary=["api"])
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.commit()
        run_cli(scene.root, "generate", "--source", "head")
        scene.git("add", "--all")
        scene.git("commit", "--allow-empty", "-m", "lock")
        return scene

    def test_verify_agrees_across_views_when_the_tree_is_clean(self):
        with self._generated() as scene:
            text = run_cli(scene.root, "verify", "--source", "head")
            data = run_cli(scene.root, "verify", "--source", "head", "--format", "json")
            self.assertEqual(text.returncode, data.returncode, text.stderr + data.stderr)
            self.assertEqual(text.returncode, 0, text.stdout + text.stderr)

    def test_verify_agrees_across_views_when_a_boundary_moved(self):
        with self._generated() as scene:
            scene.append_line("svc/api/v1.yaml", "added: true")
            scene.commit("change")
            text = run_cli(scene.root, "verify", "--source", "head")
            data = run_cli(scene.root, "verify", "--source", "head", "--format", "json")
            self.assertEqual(text.returncode, data.returncode, text.stderr + data.stderr)
            self.assertNotEqual(text.returncode, 0, text.stdout)


if __name__ == "__main__":
    unittest.main()
