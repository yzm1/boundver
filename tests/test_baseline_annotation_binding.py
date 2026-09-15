"""An acknowledged mismatch, and the annotation that belongs to it.

A baseline is a ratchet: it acknowledges findings a team has decided to carry.
An AFFECTED CONSUMERS line is not a finding of its own - it is an annotation on
a component-facet mismatch, saying who else is downstream of it. So when the
mismatch is acknowledged the annotation has nothing left to report, and if it
somehow does remain it must at least carry the severity of the facet it
annotates rather than the generic drift code.

Reaching that state takes care. A baseline pins the scope it was captured
under, so it cannot be applied under a different --transitive setting; the
annotation has to become new some other way.

Covers OBL-GRAPH-002 and OBL-GRAPH-014.
"""

from __future__ import annotations

import json
import unittest

from tests._parity import run_cli
from tests._scenarios import Scenario

BOUNDARY = 4
COMPAT = 5
COULD_NOT_CHECK = 2

OPENAPI = "openapi: 3.1.0\ninfo:\n  title: t\n  version: '1'\npaths: {}\n"

#: Each drift the obligation names, and the exit code its facet carries.
DRIFTS = {
    "boundary": (
        lambda scene: scene.append_line("svc/api/v1.yaml", "x-note: a\n"),
        BOUNDARY,
    ),
    "compat": (lambda scene: scene.git("tag", "svc-v2.0.0"), COMPAT),
}


class _Ratcheted:
    """A drift acknowledged by a baseline, then given a consumer."""

    def __init__(self, drift) -> None:
        scene = Scenario()
        scene.config["components"] = {
            "svc": {
                "path": "svc",
                "boundary": {
                    "provider": "openapi-canonical", "paths": ["api/v1.yaml"]
                },
                "version_source": {"git_tag_prefix": "svc-v"},
            },
            "sdk": {"path": "sdk", "boundary": {"provider": "leaf", "paths": []}},
        }
        scene.file("svc/api/v1.yaml", OPENAPI)
        scene.file("sdk/index.ts", "export const x = 1;\n")
        scene.commit()
        scene.git("tag", "svc-v1.0.0")
        assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
        scene.commit("lock")
        drift(scene)
        scene.commit("drift")
        self.scene = scene
        self.unbaselined = self._verify()
        written = run_cli(
            scene.root, "verify", "--source", "head", "--transitive",
            "--write-baseline", "b.json",
        )
        assert written.returncode == 0, written.stderr
        self.stored = json.loads(
            (scene.root / "b.json").read_text(encoding="utf-8")
        )
        scene.git("add", "--all")
        scene.git("commit", "-m", "baseline")
        self.acknowledged = self._verify("--baseline", "b.json")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def _verify(self, *extra):
        result = run_cli(
            self.scene.root, "verify", "--source", "head", "--transitive",
            "--format", "json", *extra,
        )
        return result.returncode, json.loads(result.stdout)

    def add_consumer(self):
        self.scene.config["components"]["svc"]["consumers"] = ["sdk"]
        self.scene.write_config()
        self.scene.commit("declare a consumer")
        return self._verify("--baseline", "b.json")


def _annotations(issues) -> list:
    return [issue for issue in issues if issue.startswith("AFFECTED CONSUMERS")]


def _mismatches(issues) -> list:
    return [issue for issue in issues if issue.startswith("MISMATCH ")]


class BaselineScopeTests(unittest.TestCase):
    """The premise, and why the obvious route to this state is closed."""

    def test_the_drift_is_reported_at_its_facet_severity(self):
        for label, (drift, severity) in DRIFTS.items():
            with self.subTest(drift=label):
                with _Ratcheted(drift) as repo:
                    code, document = repo.unbaselined
                    self.assertEqual(code, severity)
                    self.assertTrue(_mismatches(document["issues"]))

    def test_the_baseline_acknowledges_it(self):
        for label, (drift, _severity) in DRIFTS.items():
            with self.subTest(drift=label):
                with _Ratcheted(drift) as repo:
                    code, document = repo.acknowledged
                    self.assertEqual(code, 0)
                    self.assertEqual(document["issues"], [])

    def test_a_baseline_refuses_a_different_transitive_scope(self):
        """Why the annotation cannot be made new by changing the flag."""
        with _Ratcheted(DRIFTS["boundary"][0]) as repo:
            result = run_cli(
                repo.scene.root, "verify", "--source", "head",
                "--baseline", "b.json",
            )
            self.assertEqual(result.returncode, COULD_NOT_CHECK)
            self.assertIn("transitive does not match", result.stderr)


class AnnotationAcknowledgementTests(unittest.TestCase):
    """OBL-GRAPH-014: an annotation goes with the mismatch it annotates."""

    def test_an_annotation_is_acknowledged_with_its_owner(self):
        """The annotation follows the acknowledged mismatch that owns it."""
        for label, (drift, _severity) in DRIFTS.items():
            with self.subTest(drift=label):
                with _Ratcheted(drift) as repo:
                    _code, document = repo.add_consumer()
                    self.assertEqual(_annotations(document["issues"]), [])

    def test_the_owner_really_is_acknowledged(self):
        """The premise: the mismatch itself is gone from the issue set."""
        for label, (drift, _severity) in DRIFTS.items():
            with self.subTest(drift=label):
                with _Ratcheted(drift) as repo:
                    _code, document = repo.add_consumer()
                    self.assertEqual(_mismatches(document["issues"]), [])

    def test_only_the_two_new_metadata_lines_remain(self):
        with _Ratcheted(DRIFTS["boundary"][0]) as repo:
            _code, document = repo.add_consumer()
            self.assertEqual(_annotations(document["issues"]), [])
            remaining = {issue.split(":")[0] for issue in document["issues"]}
            self.assertEqual(
                remaining,
                {
                    "METADATA MISMATCH config_digest",
                    "METADATA MISMATCH svc.consumers",
                },
            )


class AnnotationSeverityTests(unittest.TestCase):
    """OBL-GRAPH-002: an unbaselined annotation keeps its facet's severity."""

    def test_the_exit_code_is_the_facet_severity(self):
        """New metadata keeps the acknowledged owner's routing severity."""
        for label, (drift, severity) in DRIFTS.items():
            with self.subTest(drift=label):
                with _Ratcheted(drift) as repo:
                    code, _document = repo.add_consumer()
                    self.assertEqual(code, severity)

    def test_the_annotation_is_suppressed_without_lowering_severity(self):
        for label, (drift, severity) in DRIFTS.items():
            with self.subTest(drift=label):
                with _Ratcheted(drift) as repo:
                    code, document = repo.add_consumer()
                    self.assertEqual(code, severity)
                    self.assertEqual(_annotations(document["issues"]), [])


if __name__ == "__main__":
    unittest.main()
