"""Maintenance contracts for pinned third-party GitHub Actions."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
USE_LINE = re.compile(r"^\s*uses:\s*([^\s#]+)(?:\s+#\s*(\S+))?\s*$")
PINNED_REF = re.compile(r"^(?P<action>[^@]+)@(?P<sha>[0-9a-f]{40})$")


class WorkflowDependencyMaintenanceTests(unittest.TestCase):
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
        import yaml

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
