"""Whether a reviewed violation can silence an unreviewed one.

A baseline is a list of violations a team has already looked at. Its whole
value rests on one property: acknowledging a violation must acknowledge that
violation and no other. If two components can share an identity, reviewing one
silently excuses the other, and the excused one never appears again.

Long display labels retain a short hash suffix, so their violation identities
remain distinct after diagnostic truncation.

Covers OBL-GRAPH-001.
"""

from __future__ import annotations

import json
import unittest

from tests._parity import run_cli
from tests._scenarios import Scenario

#: Long enough to truncate, sharing everything up to the truncation point.
SHARED_PREFIX = "c" * 497
FIRST = SHARED_PREFIX + "A" * 103
SECOND = SHARED_PREFIX + "B" * 103


class _TwoLongNames:
    """Two components whose names differ only past character 497."""

    def __init__(self) -> None:
        scene = Scenario()
        for name, tag in ((FIRST, "A"), (SECOND, "B")):
            scene.component(name, path=f"svc{tag}", boundary=["api"])
            scene.file(f"svc{tag}/api/v1.yaml", "openapi: 3.1.0\n")
        scene.commit()
        run_cli(scene.root, "generate", "--source", "head")
        scene.commit("lock")
        self.scene = scene

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def drift(self, tag: str) -> None:
        self.scene.append_line(f"svc{tag}/api/v1.yaml", f"drift {tag}\n")
        self.scene.commit(f"drift {tag}")

    def capture_baseline(self) -> dict:
        result = run_cli(
            self.scene.root, "verify", "--source", "head",
            "--write-baseline", "bv.baseline.json",
        )
        assert result.returncode == 0, result.stderr
        self.scene.git("add", "--all")
        self.scene.git("commit", "-m", "baseline")
        return json.loads(
            (self.scene.root / "bv.baseline.json").read_text(encoding="utf-8")
        )

    def verify(self, *arguments):
        return run_cli(self.scene.root, "verify", "--source", "head", *arguments)

    def mismatch_count(self) -> int:
        result = self.verify()
        text = (result.stdout or "") + (result.stderr or "")
        return sum(1 for line in text.splitlines() if "MISMATCH" in line)


class BaselineSuppressionTests(unittest.TestCase):
    """OBL-GRAPH-001: acknowledging one violation must not acknowledge another."""

    def test_the_names_differ_only_past_the_truncation_point(self):
        """The premise, asserted before anything rests on it."""
        self.assertEqual(FIRST[:497], SECOND[:497])
        self.assertNotEqual(FIRST, SECOND)
        self.assertGreater(len(FIRST), 500)

    def test_one_component_drifting_is_reported(self):
        with _TwoLongNames() as repo:
            repo.drift("A")
            self.assertEqual(repo.mismatch_count(), 2)

    def test_a_baseline_silences_the_component_it_captured(self):
        """The feature working, which is what makes the next test a problem."""
        with _TwoLongNames() as repo:
            repo.drift("A")
            repo.capture_baseline()
            self.assertEqual(
                repo.verify("--baseline", "bv.baseline.json").returncode, 0
            )

    def test_a_baseline_for_one_component_does_not_silence_the_other(self):
        """The identity suffix distinguishes names beyond the display prefix.

        The baseline is captured while only the first component has drifted.
        The second then drifts and remains visible because the truncated
        labels carry distinct identity suffixes.
        """
        with _TwoLongNames() as repo:
            repo.drift("A")
            repo.capture_baseline()
            repo.drift("B")
            self.assertNotEqual(
                repo.verify("--baseline", "bv.baseline.json").returncode, 0
            )

    def test_each_long_component_has_one_identity_per_facet(self):
        with _TwoLongNames() as repo:
            repo.drift("A")
            repo.drift("B")
            self.assertEqual(repo.mismatch_count(), 4)
            document = repo.capture_baseline()
            self.assertEqual(len(document["violations"]), 4)
            self.assertEqual({v["facet"] for v in document["violations"]},
                             {"exact", "boundary"})
            self.assertEqual(len({v["subject"] for v in document["violations"]}), 2)

    def test_shorter_names_are_unaffected(self):
        """The contrast: without truncation the two are told apart."""
        scene = Scenario()
        try:
            for name in ("alpha", "beta"):
                scene.component(name, path=name, boundary=["api"])
                scene.file(f"{name}/api/v1.yaml", "openapi: 3.1.0\n")
            scene.commit()
            run_cli(scene.root, "generate", "--source", "head")
            scene.commit("lock")
            scene.append_line("alpha/api/v1.yaml", "drift\n")
            scene.commit("drift alpha")
            run_cli(
                scene.root, "verify", "--source", "head",
                "--write-baseline", "bv.baseline.json",
            )
            scene.git("add", "--all")
            scene.git("commit", "-m", "baseline")
            self.assertEqual(
                run_cli(scene.root, "verify", "--source", "head",
                        "--baseline", "bv.baseline.json").returncode,
                0,
            )
            scene.append_line("beta/api/v1.yaml", "drift\n")
            scene.commit("drift beta")
            self.assertNotEqual(
                run_cli(scene.root, "verify", "--source", "head",
                        "--baseline", "bv.baseline.json").returncode,
                0,
            )
        finally:
            scene.close()


if __name__ == "__main__":
    unittest.main()
