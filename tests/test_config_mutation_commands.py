"""The three commands that write the config file.

`init`, `add` and `remove` are the only commands that edit a user's
declaration, which makes them the only ones where a refusal has to be complete.
A command that rejects an edit must leave the file exactly as it found it, say
why on stderr, and not print a success line first - and it must refuse before
parsing, because a config it will not write is a config it has no business
reading either.

The order of those refusals is part of the contract too. Several conditions can
hold at once, and which diagnostic a user sees decides what they do next: the
three commands do not check in the same order, and that difference is
deliberate for `init` and observable for the other two.

Covers OBL-CONFIG-039, OBL-CONFIG-045, OBL-CONFIG-046, OBL-CONFIG-047 and
OBL-CONFIG-048.
"""

from __future__ import annotations

import json
import unittest

from boundver._config import validate_config

from tests._parity import run_cli
from tests._scenarios import Scenario

USAGE = 2

#: Every non-JSON spelling find_config_file will fall back to.
NON_JSON = ("boundary.config.yaml", "boundary.config.yml", "boundary.config.toml")

YAML_CONFIG = (
    "project: scenario\n"
    "components:\n"
    "  svc:\n"
    "    path: svc\n"
    "    boundary:\n"
    "      provider: leaf\n"
    "      paths: []\n"
)

TOML_CONFIG = (
    'project = "scenario"\n'
    "[components.svc]\n"
    'path = "svc"\n'
    "[components.svc.boundary]\n"
    'provider = "leaf"\n'
    "paths = []\n"
)


def _scene(**files) -> Scenario:
    """A repository with one component and whatever extra files are named."""
    scene = Scenario()
    scene.component("svc", path="svc", provider="leaf")
    scene.file("svc/main.py", "x\n")
    for name in ("a", "b", "c,d", "e"):
        scene.file(f"sdk/{name}", "y\n")
    for name, content in files.items():
        scene.file(name.replace("__", "."), content)
    scene.commit()
    return scene


class _Attempt:
    """One command run, with the config file before and after."""

    def __init__(self, scene: Scenario, *args: str, config: str = None) -> None:
        self.scene = scene
        self.path = scene.root / (config or "boundary.config.json")
        self.before = self.path.read_bytes() if self.path.exists() else None
        self.result = run_cli(scene.root, *args)
        self.after = self.path.read_bytes() if self.path.exists() else None

    @property
    def code(self) -> int:
        return self.result.returncode

    @property
    def stdout(self) -> str:
        return self.result.stdout

    @property
    def stderr(self) -> str:
        return self.result.stderr

    def loaded(self) -> dict:
        return json.loads(self.after.decode("utf-8"))

    def assert_untouched(self, case) -> None:
        case.assertEqual(self.code, USAGE, self.stderr)
        case.assertEqual(self.after, self.before)
        case.assertEqual(self.stdout, "")
        case.assertNotEqual(self.stderr.strip(), "")


class AddPathReportingTests(unittest.TestCase):
    """OBL-CONFIG-039: the confirmation must name the stored path."""

    def test_the_boundary_paths_compose_in_a_fixed_order(self):
        """--paths splits on commas; --boundary-path values keep theirs."""
        with _scene() as scene:
            attempt = _Attempt(
                scene, "add", "sdk", "sdk", "--provider", "path-hash",
                "--paths", "a,b", "--boundary-path", "c,d",
                "--boundary-path", "e",
            )
            self.assertEqual(attempt.code, 0, attempt.stderr)
            self.assertEqual(
                attempt.loaded()["components"]["sdk"]["boundary"]["paths"],
                ["a", "b", "c,d", "e"],
            )

    def test_an_empty_selector_is_dropped_rather_than_stored(self):
        with _scene() as scene:
            attempt = _Attempt(
                scene, "add", "sdk", "sdk", "--provider", "path-hash",
                "--paths", "a, ,b",
            )
            self.assertEqual(attempt.code, 0, attempt.stderr)
            self.assertEqual(
                attempt.loaded()["components"]["sdk"]["boundary"]["paths"],
                ["a", "b"],
            )

    def test_the_stored_path_is_normalized(self):
        """The premise: the two values really do differ."""
        with _scene() as scene:
            attempt = _Attempt(
                scene, "add", "sdk", "sdk/", "--provider", "path-hash", "--paths", "a"
            )
            self.assertEqual(attempt.code, 0, attempt.stderr)
            self.assertEqual(attempt.loaded()["components"]["sdk"]["path"], "sdk")

    def test_the_confirmation_names_the_path_it_stored(self):
        """The confirmation reports the normalized value persisted to JSON."""
        with _scene() as scene:
            attempt = _Attempt(
                scene, "add", "sdk", "sdk/", "--provider", "path-hash", "--paths", "a"
            )
            stored = attempt.loaded()["components"]["sdk"]["path"]
            self.assertIn(f"at path '{stored}'", attempt.stdout)

    def test_the_raw_argument_is_not_reported_as_the_stored_path(self):
        """Both slash spellings report the normalized persisted path."""
        for spelling in ("sdk/", "sdk" + chr(92) + "sub"):
            with self.subTest(argument=spelling):
                with _scene() as scene:
                    scene.file("sdk/sub/x.py", "z\n")
                    scene.commit("sub")
                    attempt = _Attempt(
                        scene, "add", "sdk", spelling, "--provider", "leaf"
                    )
                    self.assertEqual(attempt.code, 0, attempt.stderr)
                    stored = attempt.loaded()["components"]["sdk"]["path"]
                    self.assertIn(f"at path '{stored}'", attempt.stdout)
                    self.assertNotIn(f"at path '{spelling}'", attempt.stdout)
                    self.assertNotEqual(stored, spelling)


class JsonOnlyGuardTests(unittest.TestCase):
    """OBL-CONFIG-045: a config this command cannot write is not read."""

    def _without_json(self, name: str, content: str) -> Scenario:
        """Leave only the non-JSON config in the working tree.

        Committing here would rewrite boundary.config.json, which is what the
        scenario helper does on every commit.
        """
        scene = _scene()
        (scene.root / "boundary.config.json").unlink()
        (scene.root / name).write_text(content, encoding="utf-8")
        return scene

    def test_add_refuses_every_non_json_spelling(self):
        for name in NON_JSON:
            content = TOML_CONFIG if name.endswith(".toml") else YAML_CONFIG
            with self.subTest(config=name):
                with self._without_json(name, content) as scene:
                    before = (scene.root / name).read_bytes()
                    result = run_cli(
                        scene.root, "add", "sdk", "sdk", "--provider", "leaf"
                    )
                    self.assertEqual(result.returncode, USAGE)
                    self.assertIn("`boundver add` only writes JSON configs", result.stderr)
                    self.assertEqual((scene.root / name).read_bytes(), before)
                    self.assertFalse((scene.root / "boundary.config.json").exists())
                    self.assertEqual(result.stdout, "")

    def test_remove_refuses_them_too_and_names_itself(self):
        for name in NON_JSON:
            content = TOML_CONFIG if name.endswith(".toml") else YAML_CONFIG
            with self.subTest(config=name):
                with self._without_json(name, content) as scene:
                    before = (scene.root / name).read_bytes()
                    result = run_cli(scene.root, "remove", "svc")
                    self.assertEqual(result.returncode, USAGE)
                    self.assertIn(
                        "`boundver remove` only writes JSON configs", result.stderr
                    )
                    self.assertEqual((scene.root / name).read_bytes(), before)

    def test_the_refusal_precedes_parsing(self):
        """A YAML file that could not be parsed still gets the JSON message."""
        with self._without_json("boundary.config.yaml", "this: [is: not: yaml\n") as scene:
            result = run_cli(scene.root, "add", "sdk", "sdk", "--provider", "leaf")
            self.assertEqual(result.returncode, USAGE)
            self.assertIn("only writes JSON configs", result.stderr)

    def test_the_suffix_test_is_case_insensitive(self):
        with _scene() as scene:
            result = run_cli(scene.root, "init", "--out", "custom.JSON")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((scene.root / "custom.JSON").exists())

    def test_a_json_lookalike_suffix_is_refused(self):
        for name in ("boundary.config.json.bak", "myconfig", ".json"):
            with self.subTest(config=name):
                with _scene() as scene:
                    (scene.root / name).write_text(
                        (scene.root / "boundary.config.json").read_text(
                            encoding="utf-8"
                        ),
                        encoding="utf-8",
                    )
                    attempt = _Attempt(
                        scene, "add", "sdk", "sdk", "--provider", "leaf",
                        "--config", name, config=name,
                    )
                    attempt.assert_untouched(self)
                    self.assertIn("only writes JSON configs", attempt.stderr)


class AllOrNothingTests(unittest.TestCase):
    """OBL-CONFIG-046: a rejected edit leaves nothing behind."""

    def _add(self, scene: Scenario, *extra: str) -> _Attempt:
        return _Attempt(scene, "add", *extra)

    def test_every_rejecting_branch_leaves_the_file_alone(self):
        cases = {
            "config missing": (
                ("add", "sdk", "sdk", "--config", "absent.json"), None
            ),
            "unaddressable name": (("add", "a,b", "sdk"), None),
            "name already present": (("add", "svc", "sdk"), None),
            "invalid path": (("add", "sdk", ".."), None),
            "post-edit invalid": (("add", "sdk", "nowhere"), None),
            "pre-edit invalid": (
                ("add", "sdk", "sdk"),
                lambda scene: scene.file(
                    "boundary.config.json",
                    json.dumps({"project": "p", "components": {"x": {}}}),
                ),
            ),
            "unparseable config": (
                ("add", "sdk", "sdk"),
                lambda scene: scene.file("boundary.config.json", "{not json"),
            ),
        }
        for label, (command, prepare) in cases.items():
            with self.subTest(branch=label):
                with _scene() as scene:
                    if prepare is not None:
                        prepare(scene)
                    attempt = _Attempt(scene, *command)
                    if label == "config missing":
                        self.assertEqual(attempt.code, USAGE)
                        self.assertEqual(attempt.stdout, "")
                    else:
                        attempt.assert_untouched(self)

    def test_the_two_validation_failures_are_distinguishable(self):
        """They demand opposite actions, so they must not read alike."""
        with _scene() as scene:
            scene.file(
                "boundary.config.json",
                json.dumps({"project": "p", "components": {"x": {}}}),
            )
            attempt = _Attempt(scene, "add", "sdk", "sdk")
            self.assertIn("Config is invalid (", attempt.stderr)
            self.assertNotIn("would leave an invalid config", attempt.stderr)

        with _scene() as scene:
            attempt = _Attempt(scene, "add", "sdk", "nowhere")
            self.assertIn("Adding 'sdk' would leave an invalid config", attempt.stderr)
            self.assertNotIn("Config is invalid (", attempt.stderr)

    def test_no_success_line_precedes_a_refusal(self):
        with _scene() as scene:
            attempt = _Attempt(scene, "add", "sdk", "nowhere")
            self.assertNotIn("Added component", attempt.stdout + attempt.stderr)
        with _scene() as scene:
            scene.component("sdk", path="sdk", provider="leaf")
            scene.slice("everything", components=["svc", "sdk"])
            scene.write_config()
            scene.commit("two components")
            attempt = _Attempt(scene, "remove", "absent")
            attempt.assert_untouched(self)
            self.assertNotIn("Removed component", attempt.stdout + attempt.stderr)

    def test_a_successful_edit_does_write(self):
        """The premise: these commands are capable of changing the file."""
        with _scene() as scene:
            attempt = _Attempt(scene, "add", "sdk", "sdk", "--provider", "leaf")
            self.assertEqual(attempt.code, 0, attempt.stderr)
            self.assertNotEqual(attempt.after, attempt.before)
            self.assertIn("Added component 'sdk'", attempt.stdout)


class RemoveSliceRewriteTests(unittest.TestCase):
    """OBL-CONFIG-047: exactly one transformation beyond the deletion."""

    def _repository(self) -> Scenario:
        scene = _scene()
        scene.component("sdk", path="sdk", provider="leaf")
        scene.component("app", path="app", provider="leaf", consumers=["svc"])
        scene.file("app/main.py", "z\n")
        scene.slice("first", components=["svc", "sdk", "app"])
        scene.slice("second", components=["app", "sdk"])
        scene.slice("untouched", components=["svc", "app"])
        scene.slice("closure", closure_of="app")
        scene.write_config()
        scene.commit("graph")
        return scene

    def test_the_name_is_dropped_from_every_slice_that_lists_it(self):
        """Not only the first slice that mentions it."""
        with self._repository() as scene:
            attempt = _Attempt(scene, "remove", "sdk")
            self.assertEqual(attempt.code, 0, attempt.stderr)
            slices = attempt.loaded()["slices"]
            self.assertEqual(slices["first"]["components"], ["svc", "app"])
            self.assertEqual(slices["second"]["components"], ["app"])
            self.assertEqual(slices["untouched"]["components"], ["svc", "app"])

    def test_the_surviving_order_is_preserved(self):
        """The removed name was in the middle, so order is a real question."""
        with self._repository() as scene:
            attempt = _Attempt(scene, "remove", "sdk")
            self.assertEqual(attempt.code, 0, attempt.stderr)
            self.assertEqual(
                attempt.loaded()["slices"]["first"]["components"], ["svc", "app"]
            )

    def test_nothing_else_changes(self):
        with self._repository() as scene:
            before = json.loads(
                (scene.root / "boundary.config.json").read_text(encoding="utf-8")
            )
            attempt = _Attempt(scene, "remove", "sdk")
            self.assertEqual(attempt.code, 0, attempt.stderr)
            after = attempt.loaded()
            expected = json.loads(json.dumps(before))
            del expected["components"]["sdk"]
            expected["slices"]["first"]["components"] = ["svc", "app"]
            expected["slices"]["second"]["components"] = ["app"]
            self.assertEqual(after, expected)

    def test_a_closure_slice_is_left_for_the_user(self):
        """Only an explicit member list is rewritten."""
        with self._repository() as scene:
            attempt = _Attempt(scene, "remove", "sdk")
            self.assertEqual(
                attempt.loaded()["slices"]["closure"], {"mode": "exact", "closure_of": "app"}
            )

    def test_an_incoming_consumer_edge_blocks_the_removal(self):
        """A semantic edge is refused rather than silently rewritten."""
        with self._repository() as scene:
            attempt = _Attempt(scene, "remove", "svc")
            attempt.assert_untouched(self)
            self.assertIn("references unknown consumer: svc", attempt.stderr)
            self.assertIn("Update incoming consumers", attempt.stderr)

    def test_a_duplicate_member_is_refused_by_validation(self):
        """Which is what makes removing only the first occurrence correct."""
        with self._repository() as scene:
            config = json.loads(
                (scene.root / "boundary.config.json").read_text(encoding="utf-8")
            )
            config["slices"]["first"]["components"] = ["svc", "sdk", "svc"]
            errors = validate_config(config, scene.root)
            self.assertTrue(errors)
            self.assertTrue(
                any("duplicate" in error.lower() for error in errors), errors
            )


class DiagnosticPrecedenceTests(unittest.TestCase):
    """OBL-CONFIG-048: which failure a user is told about first."""

    def test_init_checks_the_suffix_before_the_overwrite_guard(self):
        with _scene() as scene:
            (scene.root / "existing.yaml").write_text(YAML_CONFIG, encoding="utf-8")
            result = run_cli(scene.root, "init", "--out", "existing.yaml")
            self.assertEqual(result.returncode, USAGE)
            self.assertIn("only writes JSON configs", result.stderr)
            self.assertNotIn("already exists", result.stderr)

    def test_init_reports_the_overwrite_guard_for_a_json_target(self):
        """The premise: that guard does fire when the suffix is acceptable."""
        with _scene() as scene:
            result = run_cli(scene.root, "init", "--out", "boundary.config.json")
            self.assertEqual(result.returncode, USAGE)
            self.assertIn("already exists", result.stderr)

    def test_add_checks_existence_before_the_suffix(self):
        with _scene() as scene:
            result = run_cli(
                scene.root, "add", "sdk", "sdk", "--config", "missing.yaml"
            )
            self.assertEqual(result.returncode, USAGE)
            self.assertIn("Config file not found", result.stderr)
            self.assertNotIn("only writes JSON configs", result.stderr)

    def test_the_add_precedence_chain_is_fixed(self):
        """Each case makes every later condition true as well."""
        chain = [
            (
                "missing file",
                ("add", "a,b", "..", "--config", "missing.json"),
                "Config file not found",
            ),
            ("unaddressable name", ("add", "a,b", ".."), "is not addressable"),
            ("name already present", ("add", "svc", ".."), "already exists in config"),
            ("invalid path", ("add", "sdk", ".."), "Invalid component path"),
            ("post-edit invalid", ("add", "sdk", "nowhere"), "would leave an invalid"),
        ]
        for label, command, expected in chain:
            with self.subTest(branch=label):
                with _scene() as scene:
                    attempt = _Attempt(scene, *command)
                    self.assertEqual(attempt.code, USAGE)
                    self.assertIn(expected, attempt.stderr)

    def test_only_add_offers_the_init_hint(self):
        with _scene() as scene:
            added = run_cli(
                scene.root, "add", "sdk", "sdk", "--config", "missing.json"
            )
            removed = run_cli(scene.root, "remove", "svc", "--config", "missing.json")
            self.assertIn("Run: boundver init", added.stderr)
            self.assertNotIn("Run: boundver init", removed.stderr)

    def test_the_hint_is_withheld_when_init_would_not_help(self):
        """A missing non-JSON config has no compatible init command."""
        with _scene() as scene:
            result = run_cli(
                scene.root, "add", "sdk", "sdk", "--config", "missing.yaml"
            )
            self.assertNotIn("Run: boundver init", result.stderr)

    def test_a_non_json_name_gets_no_inapplicable_init_hint(self):
        with _scene() as scene:
            result = run_cli(
                scene.root, "add", "sdk", "sdk", "--config", "missing.yaml"
            )
            self.assertEqual(result.returncode, USAGE)
            self.assertNotIn("Run: boundver init", result.stderr)
            self.assertFalse((scene.root / "missing.yaml").exists())


if __name__ == "__main__":
    unittest.main()
