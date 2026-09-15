"""What exit code a baselined run reports when something new appears.

boundver's exit codes are a contract, not a log level: 4 means a boundary
moved and 5 means compat did, so a CI job can route on them without parsing
prose. A baseline is meant to silence what a team has reviewed and leave
everything else exactly as loud as it was.

The baseline suppresses reviewed findings without changing the severity of a
new finding attached to the same boundary drift.

Covers OBL-GRAPH-002.
"""

from __future__ import annotations

import unittest

from tests._parity import run_cli
from tests._scenarios import Scenario

#: The documented boundary-facet severity.
BOUNDARY = 4


class _Ratcheted:
    """A boundary mismatch a baseline acknowledges, and room for a new one.

    Every component exists from the start, so declaring a new consumer later
    changes one config field rather than adding a component, and the two runs
    being compared differ only in whether a baseline is supplied.
    """

    def __init__(self) -> None:
        scene = Scenario()
        scene.component("api", path="api", boundary=["spec"], consumers=["sdk"])
        scene.component("sdk", path="sdk", provider="leaf")
        scene.component("app", path="app", provider="leaf")
        scene.file("api/spec/v1.yaml", "openapi: 3.1.0\n")
        scene.file("sdk/index.ts", "export const x = 1;\n")
        scene.file("app/main.ts", "const y = 1;\n")
        scene.commit()
        run_cli(scene.root, "generate", "--source", "head")
        scene.commit("lock")
        scene.append_line("api/spec/v1.yaml", "drift\n")
        scene.commit("boundary drift")
        self.scene = scene

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def capture_baseline(self) -> None:
        result = run_cli(
            self.scene.root, "verify", "--source", "head",
            "--write-baseline", "bv.json",
        )
        assert result.returncode == 0, result.stderr
        self.scene.git("add", "--all")
        self.scene.git("commit", "-m", "baseline")

    def declare_new_consumer(self) -> None:
        self.scene.config["components"]["api"]["consumers"] = ["sdk", "app"]
        self.scene.commit("declare a new consumer")

    def verify(self, *arguments):
        return run_cli(self.scene.root, "verify", "--source", "head", *arguments)

    def lines(self, result) -> list:
        text = (result.stdout or "") + (result.stderr or "")
        return [line.strip() for line in text.splitlines() if line.strip()]


class RatchetSeverityTests(unittest.TestCase):
    """OBL-GRAPH-002: a baseline must not quieten what it did not acknowledge."""

    def test_a_boundary_mismatch_reports_its_facet_severity(self):
        """The premise: the code under test is 4, not 1."""
        with _Ratcheted() as repo:
            result = repo.verify()
            self.assertEqual(result.returncode, BOUNDARY, result.stdout)
            self.assertTrue(
                any("MISMATCH api.boundary" in line for line in repo.lines(result))
            )

    def test_a_baseline_silences_what_it_acknowledged(self):
        with _Ratcheted() as repo:
            repo.capture_baseline()
            self.assertEqual(repo.verify("--baseline", "bv.json").returncode, 0)

    def test_a_new_consumer_is_reported_without_a_baseline(self):
        """Both runs being compared see the same findings, baseline aside."""
        with _Ratcheted() as repo:
            repo.capture_baseline()
            repo.declare_new_consumer()
            result = repo.verify()
            self.assertEqual(result.returncode, BOUNDARY)
            lines = repo.lines(result)
            self.assertTrue(any("api.consumers" in line for line in lines))
            self.assertTrue(any("MISMATCH api.boundary" in line for line in lines))

    def test_the_baseline_does_not_lower_the_severity(self):
        """A new issue attached to boundary drift retains exit code 4.

        The unbaselined finding is an AFFECTED CONSUMERS line for a component
        whose boundary moved, so the run is still about a boundary change. A
        job routing on exit 4 sees generic drift instead.
        """
        with _Ratcheted() as repo:
            repo.capture_baseline()
            repo.declare_new_consumer()
            self.assertEqual(
                repo.verify("--baseline", "bv.json").returncode,
                repo.verify().returncode,
            )

    def test_the_new_findings_retain_boundary_severity(self):
        with _Ratcheted() as repo:
            repo.capture_baseline()
            repo.declare_new_consumer()
            baselined = repo.verify("--baseline", "bv.json")
            self.assertEqual(baselined.returncode, BOUNDARY)
            self.assertEqual(repo.verify().returncode, BOUNDARY)

            lines = repo.lines(baselined)
            self.assertTrue(any("AFFECTED CONSUMERS api" in line for line in lines))
            self.assertTrue(any("api.consumers" in line for line in lines))
            self.assertTrue(any("MISMATCH api.boundary" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
