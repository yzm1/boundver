"""The largest baseline this version writes, and the smallest it refuses.

A baseline is written by one run and read by another, so the two ends have to
agree about the last byte. The writer builds incrementally and seeds its count
at one for the trailing newline; the reader checks the encoded payload on its
own. An off-by-one between them would not be visible until a team's baseline
grew large enough, and then it would present as a file this release wrote and
cannot read.

The file is also read by people. `git diff -- .boundver-verify-baseline.json`
is only a useful review when the same content serialises the same way, so the
key order, the indent and the single trailing newline are part of the contract
rather than an accident of json.dumps.

Covers OBL-OUTPUT-030.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from boundver._baseline import (
    MAX_BASELINE_BYTES,
    BaselineError,
    dump_baseline,
    load_baseline,
)

from tests._parity import run_cli
from tests._scenarios import Scenario

#: The order docs/migration-and-ratcheting.md's diff workflow depends on.
KEY_ORDER = [
    "$schema",
    "schema",
    "project",
    "lock_schema",
    "lock_digest",
    "config_contract",
    "source",
    "components_filter",
    "facets",
    "transitive",
    "policy_digest",
    "violations",
]

#: Padding goes in components_filter, which holds sorted unique strings of at
#: most 4096 characters each - the only field of the baseline that can carry
#: two megabytes without tripping a narrower limit first.
FILLER_WIDTH = 3000

_REAL: dict = {}


def _real_baseline() -> dict:
    """One baseline as the CLI actually writes it."""
    if not _REAL:
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/main.py", "x\n")
            scene.commit()
            assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
            scene.commit("lock")
            result = run_cli(
                scene.root, "verify", "--source", "head", "--write-baseline", "b.json"
            )
            assert result.returncode == 0, result.stderr
            _REAL.update(
                json.loads((scene.root / "b.json").read_text(encoding="utf-8"))
            )
    return json.loads(json.dumps(_REAL))


def _padded(count: int, tail: int) -> dict:
    document = _real_baseline()
    items = [f"c{index:06d}" + "x" * FILLER_WIDTH for index in range(count)]
    if tail:
        items.append("z" + "x" * tail)
    document["components_filter"] = items
    return document


def _encode(document: dict) -> str:
    """What ``dump_baseline`` produces, without its size guard in the way.

    The fixture has to be able to build a document one byte over the limit,
    which the writer would refuse to serialise. Every setting here is the
    writer's own; ``test_the_fixture_encodes_the_way_the_writer_does`` holds
    the two in step.
    """
    encoder = json.JSONEncoder(
        ensure_ascii=True, allow_nan=False, indent=2, sort_keys=False
    )
    return encoder.encode(document) + "\n"


def _measure(count: int, tail: int) -> int:
    return len(_encode(_padded(count, tail)).encode("utf-8"))


def _sized(target: int) -> dict:
    """A valid baseline whose serialised form is exactly ``target`` bytes."""
    per_item = _measure(4, 0) - _measure(3, 0)
    empty = _measure(3, 0) - 3 * per_item
    count = (target - empty) // per_item
    while True:
        remainder = target - _measure(count, 0)
        if 9 <= remainder <= 4000:
            break
        count -= 1
    tail = remainder - (per_item - FILLER_WIDTH - 6)
    for _attempt in range(5):
        actual = _measure(count, tail)
        if actual == target:
            return _padded(count, tail)
        tail += target - actual
    raise AssertionError(f"could not build a baseline of exactly {target} bytes")


class SizeBoundaryTests(unittest.TestCase):
    """The last accepted byte, and the first refused one."""

    def test_the_padding_produces_a_real_baseline(self):
        """The premise: the fixture is a document the loader would take."""
        document = _padded(2, 0)
        self.assertEqual(sorted(document), sorted(_real_baseline()))
        self.assertEqual(len(document["components_filter"]), 2)
        self.assertEqual(
            document["components_filter"],
            sorted(set(document["components_filter"])),
        )

    def test_the_fixture_encodes_the_way_the_writer_does(self):
        """The premise: the sizes below are the writer's sizes."""
        document = _padded(2, 0)
        self.assertEqual(_encode(document), dump_baseline(document))

    def test_a_baseline_of_exactly_the_limit_is_written(self):
        text = dump_baseline(_sized(MAX_BASELINE_BYTES))
        self.assertEqual(len(text.encode("utf-8")), MAX_BASELINE_BYTES)

    def test_a_baseline_of_exactly_the_limit_is_read_back(self):
        document = _sized(MAX_BASELINE_BYTES)
        with tempfile.TemporaryDirectory() as directory:
            written = Path(directory) / "big.json"
            written.write_text(
                dump_baseline(document), encoding="utf-8", newline=""
            )
            self.assertEqual(written.stat().st_size, MAX_BASELINE_BYTES)
            loaded = load_baseline(written)
        self.assertEqual(
            loaded["components_filter"], document["components_filter"]
        )

    def test_one_byte_over_the_limit_is_refused_by_the_writer(self):
        with self.assertRaises(BaselineError) as raised:
            dump_baseline(_sized(MAX_BASELINE_BYTES + 1))
        self.assertIn(f"{MAX_BASELINE_BYTES}-byte storage limit", str(raised.exception))

    def test_the_trailing_newline_counts_towards_the_limit(self):
        """A body that fills the budget exactly leaves no room for it."""
        body = _encode(_sized(MAX_BASELINE_BYTES + 1))[:-1]
        self.assertEqual(len(body.encode("utf-8")), MAX_BASELINE_BYTES)
        with self.assertRaises(BaselineError):
            dump_baseline(_sized(MAX_BASELINE_BYTES + 1))

    def test_a_file_one_byte_over_the_limit_is_refused_by_the_reader(self):
        """The writer's refusal is not the only guard on the way back in."""
        text = dump_baseline(_sized(MAX_BASELINE_BYTES))
        oversized = text.replace('"z', '"zx', 1)
        self.assertEqual(len(oversized.encode("utf-8")), MAX_BASELINE_BYTES + 1)
        with tempfile.TemporaryDirectory() as directory:
            written = Path(directory) / "over.json"
            written.write_text(oversized, encoding="utf-8", newline="")
            with self.assertRaises(BaselineError) as raised:
                load_baseline(written)
        self.assertIn("file too large", str(raised.exception))
        self.assertIn(f"({MAX_BASELINE_BYTES + 1} bytes)", str(raised.exception))


class SerialisedFormTests(unittest.TestCase):
    """What `git diff` on a baseline is allowed to show."""

    def setUp(self):
        self.baseline = _real_baseline()
        self.text = dump_baseline(self.baseline)

    def test_the_same_value_serialises_identically(self):
        self.assertEqual(self.text, dump_baseline(_real_baseline()))

    def test_the_key_order_is_the_documented_one(self):
        self.assertEqual(list(json.loads(self.text)), KEY_ORDER)
        emitted = [
            line[2:].split('"')[1]
            for line in self.text.splitlines()
            if line.startswith('  "')
        ]
        self.assertEqual(emitted, KEY_ORDER)

    def test_the_indent_is_two_spaces(self):
        for line in self.text.splitlines():
            leading = len(line) - len(line.lstrip(" "))
            self.assertEqual(leading % 2, 0, repr(line[:40]))
        self.assertTrue(self.text.splitlines()[1].startswith('  "$schema"'))

    def test_there_is_exactly_one_trailing_newline(self):
        self.assertTrue(self.text.endswith("\n"))
        self.assertFalse(self.text.endswith("\n\n"))
        self.assertNotIn("\r", self.text)

    def test_the_writer_preserves_the_order_it_is_given(self):
        """Insertion order is the writer's, not the caller's."""
        shuffled = {key: self.baseline[key] for key in reversed(KEY_ORDER)}
        self.assertEqual(list(json.loads(dump_baseline(shuffled))), KEY_ORDER[::-1])

    def test_two_cli_runs_write_the_same_bytes(self):
        """The diff workflow: an unchanged verdict is an empty diff."""
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/main.py", "x\n")
            scene.commit()
            self.assertEqual(
                run_cli(scene.root, "generate", "--source", "head").returncode, 0
            )
            scene.commit("lock")
            written = []
            for name in ("first.json", "second.json"):
                result = run_cli(
                    scene.root, "verify", "--source", "head", "--write-baseline", name
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                written.append((scene.root / name).read_bytes())
            self.assertEqual(written[0], written[1])
            self.assertTrue(written[0].endswith(b"\n"))
            self.assertNotIn(b"\r\n", written[0])


if __name__ == "__main__":
    unittest.main()
