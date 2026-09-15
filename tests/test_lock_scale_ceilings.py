"""The largest config boundver accepts, against the lock its loader will read.

Two ceilings were chosen independently. The config schema admits 10,000
components. The lock loader admits 100,000 JSON value nodes. Nothing connects
them, and a generated component entry costs a fixed number of nodes, so there
is a component count above which generation succeeds and every subsequent
verify fails on the lock generation just wrote.

Finding that count needs no repository of that size. The cost per component is
a constant, and the constant is measurable from a handful of small ones; what
the arithmetic then predicts is confirmed directly, at a lowered ceiling, on a
repository small enough to build in a test.

Covers OBL-LOCKFILE-003.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import boundver
import boundver._lockfile as lockfile
import boundver._utils as utils

from tests._parity import run_cli, run_cli_in_process
from tests._scenarios import Scenario

COULD_NOT_CHECK = 2

SCHEMA = json.loads(
    (Path(boundver.__file__).parent / "boundary.config.schema.json").read_text(
        encoding="utf-8"
    )
)

#: The largest component map the shipped schema accepts.
SCHEMA_MAX_COMPONENTS = SCHEMA["properties"]["components"]["maxProperties"]

#: Component counts the cost measurement uses. Small, and not adjacent, so a
#: cost that is not actually affine cannot fit them by accident.
SAMPLE_COUNTS = (5, 10, 20)

_LOCKS: dict = {}


def _declare(scene: Scenario, count: int) -> None:
    for index in range(count):
        name = f"c{index:04d}"
        scene.component(name, path=name, boundary=["api"])
        scene.file(f"{name}/api/v1.yaml", f"openapi: 3.1.0\nid: {index}\n")


def _lock_bytes(count: int) -> bytes:
    """The lock a repository of *count* components generates, built once."""
    if count not in _LOCKS:
        with Scenario() as scene:
            _declare(scene, count)
            scene.commit()
            result = run_cli(scene.root, "generate", "--source", "head")
            assert result.returncode == 0, result.stderr
            _LOCKS[count] = (scene.root / "boundary.lock.json").read_bytes()
    return _LOCKS[count]


def _node_cost(data: bytes) -> int:
    """The smallest node ceiling at which the loader accepts *data*.

    Asking the loader is better than counting the nodes here: a second
    implementation of the walk would have to agree with the first about what
    counts as a value, and disagreeing silently is the whole failure mode this
    file is about.
    """
    low, high = 1, 1 << 20
    while low < high:
        middle = (low + high) // 2
        with patch.object(utils, "MAX_JSON_TREE_NODES", middle):
            try:
                lockfile.parse_lockfile_bytes(data)
            except Exception:
                low = middle + 1
            else:
                high = middle
    return low


def _crossing_point() -> int:
    """The largest component count whose lock still loads, by measurement."""
    costs = {count: _node_cost(_lock_bytes(count)) for count in SAMPLE_COUNTS}
    low, high = min(costs), max(costs)
    per_component = (costs[high] - costs[low]) // (high - low)
    fixed = costs[low] - per_component * low
    return (utils.MAX_JSON_TREE_NODES - fixed) // per_component


class ComponentNodeCostTests(unittest.TestCase):
    """The cost is a constant, which is what makes the extrapolation legitimate."""

    def test_the_cost_is_affine_in_the_component_count(self):
        costs = {count: _node_cost(_lock_bytes(count)) for count in SAMPLE_COUNTS}
        first, second, third = SAMPLE_COUNTS
        per_component = (costs[second] - costs[first]) / (second - first)
        fixed = costs[first] - per_component * first
        self.assertEqual(per_component, int(per_component), costs)
        self.assertEqual(costs[third], per_component * third + fixed, costs)

    def test_the_measured_cost_is_the_ceiling_the_loader_applies(self):
        """The premise: one node either side of the measurement decides it."""
        data = _lock_bytes(SAMPLE_COUNTS[-1])
        cost = _node_cost(data)
        with patch.object(utils, "MAX_JSON_TREE_NODES", cost):
            self.assertIn("components", lockfile.parse_lockfile_bytes(data))
        with patch.object(utils, "MAX_JSON_TREE_NODES", cost - 1):
            with self.assertRaises(lockfile.LockfileError):
                lockfile.parse_lockfile_bytes(data)


class SchemaVersusLoaderTests(unittest.TestCase):
    """OBL-LOCKFILE-003: generation must enforce the reader's tighter ceiling."""

    def test_the_schema_ceiling_is_not_mistaken_for_a_lock_output_guarantee(self):
        """Config validity and bounded output size are independent contracts."""
        self.assertLess(_crossing_point(), SCHEMA_MAX_COMPONENTS)

    def test_the_crossing_point_is_around_three_fifths_of_the_schema_ceiling(self):
        """Pin where it falls, so a partial widening cannot pass unnoticed."""
        crossing = _crossing_point()
        self.assertEqual(SCHEMA_MAX_COMPONENTS, 10_000)
        self.assertEqual(utils.MAX_JSON_TREE_NODES, 100_000)
        self.assertGreater(crossing, 5_000)
        self.assertLess(crossing, 6_000)

    def test_the_measured_shape_is_the_cheapest_one(self):
        """So the crossing point is an upper bound, not a typical case.

        Every component here declares one boundary selector and nothing else.
        Consumers, an external consumer list, a behavior selector and a
        version source all add nodes, so a real repository crosses sooner.
        """
        plain = _node_cost(_lock_bytes(SAMPLE_COUNTS[-1]))
        with Scenario() as scene:
            for index in range(SAMPLE_COUNTS[-1]):
                name = f"c{index:04d}"
                scene.component(
                    name, path=name, boundary=["api"], behavior=["api"],
                    consumers=[f"c{(index + 1) % SAMPLE_COUNTS[-1]:04d}"],
                    external_consumers=["mobile"],
                )
                scene.file(f"{name}/api/v1.yaml", f"openapi: 3.1.0\nid: {index}\n")
            scene.commit()
            result = run_cli(scene.root, "generate", "--source", "head")
            self.assertEqual(result.returncode, 0, result.stderr)
            richer = _node_cost((scene.root / "boundary.lock.json").read_bytes())
        self.assertGreater(richer, plain)


class SilentAtGenerationTests(unittest.TestCase):
    """What crossing the point looks like from the outside.

    The ceiling is lowered rather than the repository grown, because the
    behaviour under test is the disagreement between two limits and not the
    size of either. The component count is unchanged; only the loader's
    allowance moves, by one node, either side of the lock's own cost.
    """

    COUNT = 20

    def _at_ceiling(self, offset: int):
        with Scenario() as scene:
            _declare(scene, self.COUNT)
            scene.commit()
            ceiling = _node_cost(_lock_bytes(self.COUNT)) + offset
            with patch.object(utils, "MAX_JSON_TREE_NODES", ceiling):
                generated = run_cli_in_process(scene.root, "generate", "--source", "head")
                verified = run_cli_in_process(
                    scene.root, "verify", "--source", "working-tree"
                )
                written = (scene.root / "boundary.lock.json").exists()
        return generated, verified, written

    def test_generation_refuses_before_writing_an_unreadable_lock(self):
        generated, verified, written = self._at_ceiling(-1)
        self.assertEqual(generated.returncode, COULD_NOT_CHECK)
        self.assertIn("lock reader safety limits", generated.stderr)
        self.assertFalse(written)
        self.assertEqual(verified.returncode, COULD_NOT_CHECK, verified.stdout)
        self.assertIn("boundary.lock.json", verified.stderr)

    def test_one_more_node_of_room_makes_both_succeed(self):
        """The contrast: nothing else about this repository is wrong."""
        generated, verified, written = self._at_ceiling(0)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        self.assertTrue(written)
        self.assertEqual(verified.returncode, 0, verified.stderr)


if __name__ == "__main__":
    unittest.main()
