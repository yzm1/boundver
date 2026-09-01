"""Maintenance contracts for pinned third-party GitHub Actions."""

import re
import unittest
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode


ROOT = Path(__file__).parents[1]
USE_LINE = re.compile(r"^\s*uses:\s*([^\s#]+)(?:\s+#\s*(\S+))?\s*$")
PINNED_REF = re.compile(r"^(?P<action>[^@]+)@(?P<sha>[0-9a-f]{40})$")


class WorkflowDependencyMaintenanceTests(unittest.TestCase):
    def test_workflow_environment_keys_are_case_insensitively_unique(self):
        """Match GitHub's case-insensitive validation of ``env`` mappings."""

        def visit(node, path, location):
            if isinstance(node, MappingNode):
                for key, value in node.value:
                    key_name = key.value if isinstance(key, ScalarNode) else "<key>"
                    if key_name == "env":
                        self.assertIsInstance(
                            value,
                            MappingNode,
                            f"{path.relative_to(ROOT)}:{key.start_mark.line + 1} "
                            "env must be a mapping",
                        )
                        seen = {}
                        for env_key, _env_value in value.value:
                            if not isinstance(env_key, ScalarNode):
                                continue
                            normalized = env_key.value.casefold()
                            previous = seen.setdefault(normalized, env_key)
                            self.assertIs(
                                previous,
                                env_key,
                                f"{path.relative_to(ROOT)}:"
                                f"{env_key.start_mark.line + 1} environment key "
                                f"{env_key.value!r} duplicates {previous.value!r} "
                                f"at line {previous.start_mark.line + 1} in {location}",
                            )
                    visit(value, path, f"{location}.{key_name}")
            elif isinstance(node, SequenceNode):
                for index, value in enumerate(node.value):
                    visit(value, path, f"{location}[{index}]")

        workflow_files = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        workflow_files += sorted((ROOT / ".github" / "workflows").glob("*.yaml"))
        workflow_files.append(ROOT / "action.yml")
        for path in workflow_files:
            document = yaml.compose(
                path.read_text(encoding="utf-8"), Loader=yaml.SafeLoader
            )
            self.assertIsNotNone(document)
            visit(document, path, "$.")

    def test_external_actions_are_pinned_and_consistent(self):
        workflow_files = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        workflow_files += sorted((ROOT / ".github" / "workflows").glob("*.yaml"))
        workflow_files.append(ROOT / "action.yml")

        pins = {}
        found = 0
        for path in workflow_files:
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                match = USE_LINE.match(line)
                if match is None:
                    continue
                reference, release_label = match.groups()
                if reference.startswith(("./", "docker://")):
                    continue
                found += 1
                pinned = PINNED_REF.fullmatch(reference)
                self.assertIsNotNone(
                    pinned,
                    f"{path.relative_to(ROOT)}:{line_number} must use a full commit SHA",
                )
                self.assertIsNotNone(
                    release_label,
                    f"{path.relative_to(ROOT)}:{line_number} must retain a release label",
                )
                key = (pinned.group("action"), release_label)
                previous = pins.setdefault(key, pinned.group("sha"))
                self.assertEqual(
                    previous,
                    pinned.group("sha"),
                    f"inconsistent pin for {key[0]} ({key[1]})",
                )

        self.assertGreater(found, 0)

    def test_dependabot_updates_actions_python_and_container_dependencies(self):
        config = yaml.safe_load(
            (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        )
        ecosystems = {
            update["package-ecosystem"] for update in config.get("updates", [])
        }
        self.assertEqual(ecosystems, {"docker", "github-actions", "pip"})
        for update in config["updates"]:
            self.assertGreaterEqual(update["cooldown"]["default-days"], 7)


if __name__ == "__main__":
    unittest.main()
