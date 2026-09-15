"""Case, twice: what a selector matches, and what order a listing comes back in.

The documentation says path selectors are case-sensitive, and they are:
matching compares code points, so `API/*.YAML` selects nothing in a tree that
holds `api/v1.yaml`. Ordering is the other half of the same question. The two
user-facing discovery views use code-point order on every host even though the
lower-level bounded `Path` helper retains host-native ordering.

Covers OBL-CROSSCUTTING-001.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from boundver._utils import _bounded_sorted_paths

from tests._parity import run_cli
from tests._scenarios import Scenario

COULD_NOT_CHECK = 2

#: Names chosen so byte order and case-folded order disagree: 'a' sorts after
#: every uppercase letter by code point and before 'B' when case is folded.
MIXED = ("Beta", "Gamma", "alpha")

def _discovered(scene: Scenario):
    """Both views of one discovery run."""
    document = json.loads(run_cli(scene.root, "discover", "--format", "json").stdout)
    text = run_cli(scene.root, "discover").stdout
    listed = [
        line.strip()[2:].split(":", 1)[0]
        for line in text.splitlines()
        if line.startswith("  - ")
    ]
    return list(document["components"]), listed


def _mixed_case_tree(scene: Scenario) -> None:
    scene.config["components"] = {
        "root": {"path": "keep", "boundary": {"provider": "leaf", "paths": []}}
    }
    scene.file("keep/x.py", "x = 1\n")
    for name in MIXED:
        scene.file(
            f"{name}/package.json",
            '{"name": "%s", "version": "1.0.0"}\n' % name.lower(),
        )
        scene.file(f"{name}/index.js", "module.exports = 1;\n")
    scene.commit()


class SelectorCaseTests(unittest.TestCase):
    """Matching compares code points, on every platform."""

    def _generated(self, selector: str):
        with Scenario() as scene:
            scene.component("svc", path="svc", boundary=[selector])
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
            scene.commit()
            return run_cli(scene.root, "generate", "--source", "head")

    def test_a_selector_in_the_wrong_case_selects_nothing(self):
        result = self._generated("API/*.YAML")
        self.assertEqual(result.returncode, COULD_NOT_CHECK, result.stdout)
        self.assertIn("matched no tracked files", result.stderr)

    def test_the_same_selector_in_the_right_case_selects_the_file(self):
        """The contrast: the refusal is the case, not the selector."""
        self.assertEqual(self._generated("api/*.yaml").returncode, 0)

    def test_a_partially_wrong_case_selects_nothing_either(self):
        for selector in ("Api/*.yaml", "api/*.Yaml", "api/V1.yaml"):
            with self.subTest(selector=selector):
                self.assertEqual(
                    self._generated(selector).returncode, COULD_NOT_CHECK
                )


class ListingOrderTests(unittest.TestCase):
    """OBL-CROSSCUTTING-001: one byte order, whatever the host folds."""

    def test_git_lists_tracked_files_in_byte_order(self):
        """The premise and the oracle: Git's own order is the target."""
        with Scenario() as scene:
            _mixed_case_tree(scene)
            tracked = [
                line for line in scene.git("ls-files").splitlines()
                if "/" in line and line.endswith("index.js")
            ]
            self.assertEqual(tracked, sorted(tracked))

    def test_the_json_view_lists_components_in_byte_order(self):
        with Scenario() as scene:
            _mixed_case_tree(scene)
            from_json, _from_text = _discovered(scene)
            self.assertEqual(from_json, sorted(MIXED))

    def test_the_text_view_lists_components_in_byte_order(self):
        """The human view uses the same stable order as machine output."""
        with Scenario() as scene:
            _mixed_case_tree(scene)
            _from_json, from_text = _discovered(scene)
            self.assertEqual(from_text, sorted(MIXED))

    def test_the_two_views_name_the_same_components(self):
        """Pin the scope: what differs is the order and nothing else."""
        with Scenario() as scene:
            _mixed_case_tree(scene)
            from_json, from_text = _discovered(scene)
            self.assertEqual(set(from_json), set(MIXED))
            self.assertEqual(set(from_text), set(from_json))

    def test_the_bounded_sort_agrees_with_byte_order_only_where_case_is_kept(self):
        """The mechanism, stated so it reads the same on either host.

        _bounded_sorted_paths sorts Path objects, and Path comparison goes
        through os.path.normcase. Where that is the identity the two orders
        agree; where it folds case they do not.
        """
        ordered = [
            path.name for path in _bounded_sorted_paths(
                (Path(name) for name in MIXED),
                max_paths=len(MIXED),
                exceeded_message="unused",
            )
        ]
        folds_case = os.path.normcase("A") != "A"
        self.assertEqual(ordered != sorted(MIXED), folds_case, ordered)


if __name__ == "__main__":
    unittest.main()
