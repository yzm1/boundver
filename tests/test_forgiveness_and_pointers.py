"""What a baseline stops forgiving, and how a pointer to nothing is written.

Version metadata is forgiven while the compat mismatch it accompanies is
itself acknowledged - carrying the version bump is the point of carrying the
compat break. The conjunction that matters is the second half: once the compat
mismatch is repaired, the metadata has nothing to hide behind and must be
reported. Both halves are one edit apart, so the test walks the sequence.

The pointer question is smaller and entirely about reading: a change to a whole
document has an empty RFC 6901 pointer, and printing an empty string would put
two spaces in the line and name nothing.

Covers OBL-FACETS-007 and OBL-OUTPUT-022.
"""

from __future__ import annotations

import json
import re
import unittest

from tests._parity import run_cli
from tests._scenarios import Scenario

DRIFT = 1
COMPAT = 5

OPENAPI = "openapi: 3.1.0\ninfo:\n  title: t\n  version: '1'\npaths: {}\n"

#: The two metadata lines a live baselined compat mismatch forgives.
FORGIVEN = ("METADATA MISMATCH svc.version", "METADATA MISMATCH svc.semver")

#: How an empty pointer must be written.
WHOLE_DOCUMENT = "<document>"


def _lock(scene, message: str) -> None:
    result = run_cli(scene.root, "generate", "--source", "head")
    assert result.returncode == 0, result.stderr
    scene.commit(message)


class _Versioned:
    """A component whose version comes from a Git tag."""

    def __init__(self) -> None:
        scene = Scenario()
        scene.config["components"] = {
            "svc": {
                "path": "svc",
                "boundary": {
                    "provider": "openapi-canonical", "paths": ["api/v1.yaml"]
                },
                "version_source": {"git_tag_prefix": "svc-v"},
            }
        }
        scene.file("svc/api/v1.yaml", OPENAPI)
        scene.commit()
        scene.git("tag", "svc-v1.0.0")
        _lock(scene, "lock")
        self.scene = scene

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def verify(self, *extra):
        result = run_cli(
            self.scene.root, "verify", "--source", "head", "--format", "json", *extra
        )
        return result.returncode, json.loads(result.stdout)

    def capture(self) -> list:
        written = run_cli(
            self.scene.root, "verify", "--source", "head",
            "--write-baseline", "b.json",
        )
        assert written.returncode == 0, written.stderr
        stored = json.loads(
            (self.scene.root / "b.json").read_text(encoding="utf-8")
        )
        self.scene.git("add", "--all")
        self.scene.git("commit", "-m", "baseline")
        return stored["violations"]


class CompatForgivenessTests(unittest.TestCase):
    """OBL-FACETS-007: forgiven only while the compat mismatch is live."""

    def test_a_major_bump_reports_all_three_lines(self):
        """The premise: the metadata lines exist to be forgiven."""
        with _Versioned() as repo:
            repo.scene.git("tag", "svc-v2.0.0")
            code, document = repo.verify()
            self.assertEqual(code, COMPAT)
            self.assertTrue(
                any(issue.startswith("MISMATCH svc.compat") for issue in document["issues"])
            )
            for prefix in FORGIVEN:
                self.assertTrue(
                    any(issue.startswith(prefix) for issue in document["issues"]),
                    prefix,
                )

    def test_a_baseline_over_the_compat_mismatch_forgives_them(self):
        with _Versioned() as repo:
            repo.scene.git("tag", "svc-v2.0.0")
            stored = repo.capture()
            self.assertEqual(
                [(entry["kind"], entry["subject"], entry["facet"]) for entry in stored],
                [("component-facet", "svc", "compat")],
            )
            code, document = repo.verify("--baseline", "b.json")
            self.assertEqual(code, 0)
            self.assertEqual(document["issues"], [])

    def test_repairing_compat_reports_them_as_new(self):
        """The apply-time half: the metadata has nothing left to hide behind."""
        with _Versioned() as repo:
            repo.scene.git("tag", "svc-v2.0.0")
            repo.capture()
            repo.scene.git("tag", "-d", "svc-v2.0.0")
            repo.scene.git("tag", "svc-v1.5.0")
            code, document = repo.verify("--baseline", "b.json")
            self.assertEqual(code, DRIFT)
            reported = [issue.split(":")[0] for issue in document["issues"]]
            self.assertEqual(sorted(reported), sorted(FORGIVEN))

    def test_the_repaired_compat_mismatch_really_is_gone(self):
        """So the two lines above are new rather than left over."""
        with _Versioned() as repo:
            repo.scene.git("tag", "svc-v2.0.0")
            repo.capture()
            repo.scene.git("tag", "-d", "svc-v2.0.0")
            repo.scene.git("tag", "svc-v1.5.0")
            _code, document = repo.verify("--baseline", "b.json")
            self.assertFalse(
                any("svc.compat" in issue for issue in document["issues"])
            )
            without = repo.verify()
            self.assertFalse(
                any(issue.startswith("MISMATCH svc.compat") for issue in without[1]["issues"])
            )


class StructuralPointerTests(unittest.TestCase):
    """OBL-OUTPUT-022: a pointer to the whole document has a name."""

    def _rendered(self, edit) -> list:
        with Scenario() as scene:
            scene.config["components"] = {
                "svc": {
                    "path": "svc",
                    "boundary": {
                        "provider": "openapi-canonical", "paths": ["api/*.yaml"]
                    },
                }
            }
            scene.file("svc/api/v1.yaml", OPENAPI)
            scene.file("svc/api/v2.yaml", OPENAPI.replace("'1'", "'2'"))
            scene.commit()
            _lock(scene, "lock")
            base = scene.head()
            edit(scene)
            scene.commit("edit")
            _lock(scene, "relock")
            result = run_cli(
                scene.root, "review", "--base", base, "--target", scene.head()
            )
            assert result.returncode == 0, result.stderr
            return [
                line for line in result.stdout.splitlines()
                if re.match(r"^\s+(added|removed|changed) ", line)
            ]

    def test_a_whole_document_change_names_the_document(self):
        def edit(scene):
            (scene.root / "svc" / "api" / "v2.yaml").unlink()
            scene.file("svc/api/v3.yaml", OPENAPI.replace("'1'", "'3'"))

        lines = self._rendered(edit)
        self.assertEqual(
            sorted(line.strip() for line in lines),
            [
                "added <document>: None -> object",
                "removed <document>: object -> None",
            ],
        )

    def test_a_pointer_inside_a_document_is_written_verbatim(self):
        """The contrast: a non-empty pointer is not replaced."""
        lines = self._rendered(
            lambda scene: scene.append_line("svc/api/v2.yaml", "x-note: a\n")
        )
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("/x-note", lines[0])
        self.assertNotIn(WHOLE_DOCUMENT, lines[0])

    def test_no_change_line_carries_a_doubled_space(self):
        """Which is what printing an empty pointer would produce."""
        for label, edit in (
            ("whole document", lambda scene: (
                scene.root / "svc" / "api" / "v2.yaml"
            ).unlink()),
            ("inside a document", lambda scene: scene.append_line(
                "svc/api/v2.yaml", "x-note: a\n"
            )),
        ):
            with self.subTest(change=label):
                for line in self._rendered(edit):
                    self.assertNotIn("  ", line.strip(), line)


if __name__ == "__main__":
    unittest.main()
