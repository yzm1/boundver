"""Tests for the scenario builder itself.

Fixture code that quietly does the wrong thing is worse than no fixture, because
every test built on it still passes. The line-ending case is the clear one: a
developer machine with ``core.autocrlf=true`` converts CRLF to LF on the way
into the index, so a CRLF test asserts nothing there and asserts something else
on a Linux runner. These tests pin the guarantees the obligation suite relies
on, so a regression in the fixture surfaces here rather than as a few hundred
tests that pass without exercising anything.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from tests._scenarios import (
    LF,
    PLACEHOLDER_SUBMODULE_OID,
    SOURCE_MODES,
    Scenario,
    digest_is_stable_under,
    requires_symlinks,
)

CRLF = bytes([13, 10])


def _service(scene: Scenario) -> Scenario:
    """The smallest useful shape: one component with a declared boundary."""
    scene.component("svc", path="svc", boundary=["api"], behavior=["api", "cfg.json"])
    scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
    scene.json_file("svc/cfg.json", {"retries": 3})
    return scene


class LineEndingPinningTests(unittest.TestCase):
    """The host's global Git configuration must not reach a scenario."""

    def test_crlf_content_reaches_the_index_verbatim(self):
        with Scenario() as scene:
            _service(scene).commit()
            scene.to_crlf("svc/api/v1.yaml")
            scene.commit("crlf")
            self.assertIn(
                CRLF,
                scene.blob("svc/api/v1.yaml"),
                "Git normalized CRLF the fixture wrote",
            )

    def test_the_repository_pins_autocrlf_regardless_of_the_host(self):
        with Scenario() as scene:
            self.assertEqual(scene.git("config", "core.autocrlf"), "false")

    def test_the_escape_hatch_restores_conversion(self):
        with Scenario(autocrlf="true") as scene:
            _service(scene).commit()
            scene.to_crlf("svc/api/v1.yaml")
            scene.commit("crlf")
            self.assertNotIn(CRLF, scene.blob("svc/api/v1.yaml"))

    def test_attributes_live_outside_the_tracked_tree(self):
        """A tracked .gitattributes would change every digest under test."""
        with Scenario() as scene:
            _service(scene).commit()
            attributes = scene.root / ".git" / "info" / "attributes"
            self.assertEqual(attributes.read_bytes(), b"* -text" + LF)
            self.assertNotIn(".gitattributes", scene.git("ls-files"))


class SourceModeTests(unittest.TestCase):
    """Differential obligations compare head, index and working-tree."""

    def test_every_mode_agrees_on_a_clean_tree(self):
        with Scenario() as scene:
            _service(scene).commit()
            digests = {
                mode: scene.digest("svc", "exact", source=mode) for mode in SOURCE_MODES
            }
            self.assertEqual(len(set(digests.values())), 1, digests)

    def test_staging_and_editing_separates_all_three(self):
        with Scenario() as scene:
            _service(scene).commit()
            scene.append_line("svc/api/v1.yaml", "# staged")
            scene.stage()
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n# working only\n")
            digests = {
                mode: scene.digest("svc", "exact", source=mode) for mode in SOURCE_MODES
            }
            self.assertEqual(len(set(digests.values())), 3, digests)

    def test_staging_one_path_leaves_the_others_alone(self):
        with Scenario() as scene:
            _service(scene).commit()
            scene.append_line("svc/api/v1.yaml", "# one")
            scene.append_line("svc/cfg.json", "")
            scene.stage("svc/api/v1.yaml")
            staged = scene.git("diff", "--cached", "--name-only").splitlines()
            self.assertEqual(staged, ["svc/api/v1.yaml"])


class GitStateTests(unittest.TestCase):
    """Modes and entry kinds the digest layer has to cope with."""

    def test_a_gitlink_lands_as_mode_160000(self):
        with Scenario() as scene:
            _service(scene).commit()
            scene.gitlink("svc/vendor/sub")
            fields = scene.git("ls-files", "--stage", "svc/vendor/sub").split()
            self.assertEqual(fields[0], "160000")
            self.assertEqual(fields[1], PLACEHOLDER_SUBMODULE_OID)

    def test_the_placeholder_submodule_id_is_not_the_null_object(self):
        """Git refuses a null object id in a cache entry."""
        self.assertNotEqual(PLACEHOLDER_SUBMODULE_OID, "0" * 40)

    @requires_symlinks
    def test_a_symlink_lands_as_mode_120000(self):
        with Scenario() as scene:
            _service(scene).commit()
            scene.symlink("svc/link.yaml", "api/v1.yaml")
            scene.commit("symlink")
            mode = scene.git("ls-files", "--stage", "svc/link.yaml").split()[0]
            self.assertEqual(mode, "120000")

    @unittest.skipIf(os.name == "nt", "Windows records no executable bit")
    def test_an_executable_file_lands_as_mode_100755(self):
        with Scenario() as scene:
            _service(scene)
            scene.file("svc/run.sh", "#!/bin/sh\n", executable=True)
            self.assertEqual(
                (scene.root / "svc" / "run.sh").stat().st_mode & 0o777,
                0o700,
            )
            scene.commit()
            mode = scene.git("ls-files", "--stage", "svc/run.sh").split()[0]
            self.assertEqual(mode, "100755")

    @unittest.skipIf(os.name == "nt", "Windows has no POSIX permission bits")
    def test_a_regular_file_is_owner_only(self):
        with Scenario() as scene:
            scene.file("fixture.txt", "ordinary fixture data\n")
            self.assertEqual(
                (scene.root / "fixture.txt").stat().st_mode & 0o777,
                0o600,
            )

    def test_a_transform_git_normalizes_away_still_commits(self):
        """An empty commit is the observation, not a fixture failure."""
        with Scenario(autocrlf="true") as scene:
            _service(scene).commit()
            before = scene.head()
            scene.to_crlf("svc/api/v1.yaml")
            scene.commit("no visible change")
            self.assertNotEqual(scene.head(), before)
            self.assertEqual(
                scene.git("rev-parse", "HEAD^{tree}"),
                scene.git("rev-parse", before + "^{tree}"),
            )


class TransformTests(unittest.TestCase):
    """Metamorphic transforms must change exactly what they claim to."""

    def test_reindenting_preserves_the_value(self):
        with Scenario() as scene:
            _service(scene).commit()
            path = scene.root / "svc" / "cfg.json"
            before_bytes = path.read_bytes()
            before = json.loads(path.read_text(encoding="utf-8"))
            scene.reindent_json("svc/cfg.json", indent=8)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), before)
            self.assertNotEqual(path.read_bytes(), before_bytes)

    def test_reordering_keys_preserves_the_value(self):
        with Scenario() as scene:
            scene.component("cfg", path="conf", boundary=["c.json"])
            scene.json_file("conf/c.json", {"a": 1, "b": 2, "c": 3})
            scene.commit()
            text = (scene.root / "conf" / "c.json").read_text(encoding="utf-8")
            self.assertLess(text.index("a"), text.index("c"))
            scene.reorder_json_keys("conf/c.json")
            text = (scene.root / "conf" / "c.json").read_text(encoding="utf-8")
            self.assertEqual(json.loads(text), {"a": 1, "b": 2, "c": 3})
            self.assertLess(text.index("c"), text.index("a"))

    def test_a_json_canonical_boundary_ignores_reformatting(self):
        with Scenario() as scene:
            scene.component(
                "cfg", path="conf", provider="json-canonical", boundary=["c.json"]
            )
            scene.json_file("conf/c.json", {"b": 2, "a": 1})
            scene.commit()

            def reformat(target: Scenario) -> None:
                target.reindent_json("conf/c.json", indent=8)
                target.reorder_json_keys("conf/c.json")

            self.assertTrue(
                digest_is_stable_under(scene, "cfg", "boundary", reformat)
            )

    def test_appending_to_a_declared_file_moves_the_boundary(self):
        with Scenario() as scene:
            _service(scene).commit()
            self.assertFalse(
                digest_is_stable_under(
                    scene,
                    "svc",
                    "boundary",
                    lambda target: target.append_line("svc/api/v1.yaml", "# added"),
                )
            )

    def test_an_undeclared_file_moves_exact_but_not_boundary(self):
        with Scenario() as scene:
            _service(scene).commit()
            boundary_before = scene.digest("svc", "boundary")
            exact_before = scene.digest("svc", "exact")
            scene.file("svc/notes.md", "nothing declared selects this\n")
            scene.commit("notes")
            self.assertEqual(scene.digest("svc", "boundary"), boundary_before)
            self.assertNotEqual(scene.digest("svc", "exact"), exact_before)

    def test_renaming_and_removing_take_effect(self):
        with Scenario() as scene:
            _service(scene)
            scene.file("svc/notes.md", "x\n")
            scene.commit()
            scene.rename("svc/notes.md", "svc/renamed.md")
            scene.commit("rename")
            self.assertIn("svc/renamed.md", scene.git("ls-files").splitlines())
            scene.remove("svc/renamed.md")
            scene.commit("remove")
            self.assertNotIn("svc/renamed.md", scene.git("ls-files").splitlines())


class DeclarationTests(unittest.TestCase):
    """The builder has to emit a configuration boundver accepts."""

    def test_leaf_and_implicit_providers_need_no_declared_paths(self):
        with Scenario() as scene:
            scene.component("leafsvc", path="leafsvc", provider="leaf")
            scene.component("worker", path="worker", provider="implicit")
            scene.file("leafsvc/main.py", "print(1)\n")
            scene.file("worker/main.py", "print(2)\n")
            scene.commit()
            for name in ("leafsvc", "worker"):
                self.assertIsNotNone(scene.digest(name, "exact"))

    def test_a_slice_can_be_declared_by_consumer_closure(self):
        with Scenario() as scene:
            scene.component("api", path="api", boundary=["spec"], consumers=["sdk"])
            scene.component("sdk", path="sdk", provider="leaf", consumers=["app"])
            scene.component("app", path="app", provider="leaf")
            scene.slice("downstream", mode="exact", closure_of="api")
            scene.file("api/spec/v1.yaml", "openapi: 3.1.0\n")
            scene.file("sdk/index.ts", "export const x = 1;\n")
            scene.file("app/main.ts", "const y = 2;\n")
            scene.commit()
            members = scene.generate()["slices"]["downstream"]["components"]
            self.assertEqual(members, ["api", "app", "sdk"])
            self.assertIsNotNone(scene.slice_digest("downstream"))

    def test_external_consumers_reach_the_lockfile(self):
        with Scenario() as scene:
            scene.component(
                "api", path="api", provider="leaf", external_consumers=["mobile"]
            )
            scene.file("api/main.py", "print(1)\n")
            scene.commit()
            entry = scene.generate()["components"]["api"]
            self.assertEqual(entry["external_consumers"], ["mobile"])

    def test_the_written_config_is_the_config_that_ran(self):
        with Scenario() as scene:
            _service(scene).commit()
            written = json.loads(
                (scene.root / "boundary.config.json").read_text(encoding="utf-8")
            )
            self.assertEqual(written, scene.config)


class LifecycleTests(unittest.TestCase):
    def test_leaving_the_context_removes_the_directory(self):
        with Scenario() as scene:
            root = scene.root
            _service(scene).commit()
            self.assertTrue(root.is_dir())
        self.assertFalse(Path(root).exists())

    def test_two_scenarios_do_not_share_a_directory(self):
        with Scenario() as first, Scenario() as second:
            self.assertNotEqual(first.root, second.root)


if __name__ == "__main__":
    unittest.main()
