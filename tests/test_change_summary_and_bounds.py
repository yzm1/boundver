"""Saying what did not change, and refusing what cannot be described.

A change summary is read as a claim about every facet, so the difference
between "unchanged" and "never computed" matters: a component with no
declared boundary has not had an unchanged boundary, it has had none. The
summary is built from the list of facets that moved, which does not carry
enough to tell those apart.

The other two questions here are about refusing early. A slice that resolves
to no members has no meaningful fingerprint, and metadata is accepted or
refused on the size of its canonical form rather than on a cheaper count
taken along the way.

Covers OBL-HASHING-006, OBL-HASHING-007 and OBL-HASHING-107.
"""

from __future__ import annotations

import json
import unittest

from boundver._hashing import canonical_json
from boundver.providers import (
    MAX_PROVIDER_METADATA_BYTES,
    MAX_PROVIDER_METADATA_DEPTH,
    MAX_PROVIDER_METADATA_NODES,
    _metadata_error,
)

from tests._parity import run_cli
from tests._scenarios import Scenario

COULD_NOT_CHECK = 2

#: The claim used when both declared facets exist at the compared endpoints.
UNCHANGED_CLAIM = "declared behavior and boundary artifacts are unchanged"
ABSENT_CLAIM = "no behavior or boundary artifact is declared"


def _diffed(build, edit) -> dict:
    """Generate, edit, regenerate, and return the CLI's diff document."""
    with Scenario() as scene:
        build(scene)
        scene.commit()
        assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
        (scene.root / "old.json").write_bytes(
            (scene.root / "boundary.lock.json").read_bytes()
        )
        before = json.loads((scene.root / "old.json").read_text(encoding="utf-8"))
        edit(scene)
        scene.commit("edit")
        assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
        result = run_cli(
            scene.root, "diff", "old.json", "boundary.lock.json", "--format", "json"
        )
        assert result.returncode == 0, result.stderr
        return before, json.loads(result.stdout)


def _leaf(scene) -> None:
    scene.component("leafy", path="leafy", provider="leaf")
    scene.file("leafy/index.ts", "export const x = 1;\n")


def _with_boundary(scene) -> None:
    scene.component("svc", path="svc", boundary=["api"])
    scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
    scene.file("svc/impl.ts", "export const x = 1;\n")


class ChangeSummaryTests(unittest.TestCase):
    """OBL-HASHING-007: absent is not the same as unchanged."""

    def test_a_leaf_component_has_no_behavior_or_boundary_digest(self):
        """The premise: both are null, on both sides."""
        before, document = _diffed(
            _leaf, lambda scene: scene.append_line("leafy/index.ts", "export const y = 2;\n")
        )
        fingerprints = before["components"]["leafy"]["fingerprints"]
        self.assertIsNone(fingerprints["behavior"])
        self.assertIsNone(fingerprints["boundary"])
        self.assertEqual(
            [entry["name"] for entry in document["components"]["changed"]], ["leafy"]
        )
        self.assertEqual(
            list(document["components"]["changed"][0]["changed_facets"]), ["exact"]
        )

    def test_a_facet_that_was_never_computed_is_not_called_unchanged(self):
        _before, document = _diffed(
            _leaf, lambda scene: scene.append_line("leafy/index.ts", "export const y = 2;\n")
        )
        summary = document["components"]["changed"][0]["summary"]
        self.assertNotIn(UNCHANGED_CLAIM, summary)

    def test_the_summary_names_absent_facets(self):
        _before, document = _diffed(
            _leaf, lambda scene: scene.append_line("leafy/index.ts", "export const y = 2;\n")
        )
        self.assertEqual(
            document["components"]["changed"][0]["summary"],
            "implementation-only by declaration: exact content changed; "
            + ABSENT_CLAIM,
        )

    def test_the_same_summary_is_correct_where_the_facets_exist(self):
        """The contrast: with a real boundary, the claim is true."""
        before, document = _diffed(
            _with_boundary,
            lambda scene: scene.append_line("svc/impl.ts", "export const y = 2;\n"),
        )
        fingerprints = before["components"]["svc"]["fingerprints"]
        self.assertIsNotNone(fingerprints["boundary"])
        changed = document["components"]["changed"][0]
        self.assertEqual(list(changed["changed_facets"]), ["exact"])
        self.assertIn("declared boundary artifact is unchanged", changed["summary"])
        self.assertIn("no behavior artifact is declared", changed["summary"])


class EmptySliceTests(unittest.TestCase):
    """OBL-HASHING-006: a slice with no members is refused, not fingerprinted."""

    def _generated(self, definition):
        with Scenario() as scene:
            scene.component("svc", path="svc", boundary=["api"])
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
            scene.config["slices"] = {"s": definition}
            scene.commit()
            result = run_cli(scene.root, "generate", "--source", "head")
            lock = None
            if result.returncode == 0:
                lock = json.loads(
                    (scene.root / "boundary.lock.json").read_text(encoding="utf-8")
                )
            return result, lock

    def test_a_closure_of_an_unknown_seed_is_refused_by_name(self):
        result, _lock = self._generated({"closure_of": "missing", "mode": "exact"})
        self.assertEqual(result.returncode, COULD_NOT_CHECK)
        self.assertIn("closure_of references unknown component: missing", result.stderr)

    def test_an_empty_member_list_is_refused_too(self):
        result, _lock = self._generated({"components": [], "mode": "exact"})
        self.assertEqual(result.returncode, COULD_NOT_CHECK)
        self.assertIn("should be non-empty", result.stderr)

    def test_a_closure_of_a_real_seed_produces_one_member(self):
        """The contrast: a closure always contains its own seed."""
        result, lock = self._generated({"closure_of": "svc", "mode": "exact"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lock["slices"]["s"]["components"], ["svc"])
        self.assertEqual(len(lock["slices"]["s"]["fingerprint"]), 64)

    def test_no_slice_ever_carries_the_empty_map_fingerprint(self):
        """The constant the obligation warns about, named so it cannot appear."""
        from boundver._hashing import sha256_hex

        forbidden = sha256_hex(canonical_json({}))
        _result, lock = self._generated({"closure_of": "svc", "mode": "exact"})
        self.assertNotEqual(lock["slices"]["s"]["fingerprint"], forbidden)


class MetadataCeilingTests(unittest.TestCase):
    """OBL-HASHING-107: the canonical form is what is measured."""

    def _key_overhead(self) -> int:
        return len(canonical_json({"k": ""}).encode("utf-8"))

    def test_the_byte_ceiling_is_inclusive(self):
        exact = {"k": "x" * (MAX_PROVIDER_METADATA_BYTES - self._key_overhead())}
        self.assertEqual(
            len(canonical_json(exact).encode("utf-8")), MAX_PROVIDER_METADATA_BYTES
        )
        self.assertIsNone(_metadata_error(exact))

    def test_one_byte_over_is_refused_by_the_json_limit(self):
        over = {"k": "x" * (MAX_PROVIDER_METADATA_BYTES - self._key_overhead() + 1)}
        self.assertEqual(
            _metadata_error(over),
            f"metadata exceeds the {MAX_PROVIDER_METADATA_BYTES}-byte JSON limit",
        )

    def test_a_numeric_payload_is_measured_although_it_holds_no_strings(self):
        """The obligation's case, caught by the ceiling that speaks first."""
        payload = {"n": list(range(MAX_PROVIDER_METADATA_NODES))}
        self.assertGreater(len(canonical_json(payload).encode("utf-8")), 500_000)
        self.assertEqual(
            _metadata_error(payload),
            f"metadata exceeds the {MAX_PROVIDER_METADATA_NODES}-value limit",
        )

    def test_the_depth_ceiling_is_inclusive(self):
        def nest(levels):
            root = value = {}
            for _ in range(levels - 1):
                value["k"] = {}
                value = value["k"]
            return root

        deepest = MAX_PROVIDER_METADATA_DEPTH
        self.assertIsNone(_metadata_error(nest(deepest + 1)))
        self.assertEqual(
            _metadata_error(nest(deepest + 2)),
            f"metadata exceeds the {MAX_PROVIDER_METADATA_DEPTH}-level nesting limit",
        )


if __name__ == "__main__":
    unittest.main()
