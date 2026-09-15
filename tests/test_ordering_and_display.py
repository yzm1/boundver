"""Two orderings that decide a name, and one rendering that hides a change.

Discovery has to pick which of two directories called `svc` gets the name
`svc` and which gets `svc-2`. It picks by sorting Path objects, and Path
comparison is neither the byte order of the repository-relative path nor the
same rule on every host: it compares a tuple of parts, and folds case on
Windows. So the generated config - and therefore every component identity in
the lock - can depend on which machine ran `discover`.

The display question is the mirror image. A change line shortens both digests
to twelve characters, so two digests that agree on a prefix render as an
arrow from a value to itself. Two commands draw that arrow. `diff` prints it
bare, and `why` prints it in a fingerprint table whose every row ends in a
marker, so the same colliding pair reads differently in the two places, and
each rendering has to be exercised on its own.

Covers OBL-HASHING-019, OBL-HASHING-020 and OBL-HASHING-117.
"""

from __future__ import annotations

import copy
import io
import json
import os
import re
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

import boundver._output as output
from boundver._facet_policy import facet_policy_payload
from boundver._utils import FACETS, _short

from tests._parity import cli_json, run_cli
from tests._scenarios import Scenario

MANIFEST = '{"name": "%s", "version": "1.0.0"}\n'

def _discovered(parents) -> dict:
    """The component map discovery produces for two competing directories."""
    with Scenario() as scene:
        scene.config["components"] = {
            "root": {"path": "keep", "boundary": {"provider": "leaf", "paths": []}}
        }
        scene.file("keep/x.py", "x = 1\n")
        for parent in parents:
            scene.file(f"{parent}/svc/package.json", MANIFEST % parent.lower())
            scene.file(f"{parent}/svc/index.js", "module.exports = 1;\n")
        scene.commit()
        tracked = [
            line for line in scene.git("ls-files").splitlines()
            if line.endswith("svc/package.json")
        ]
        result = run_cli(scene.root, "discover", "--format", "json")
        assert result.returncode == 0, result.stderr
        document = json.loads(result.stdout)
        return {
            name: entry["path"] for name, entry in document["components"].items()
        }, tracked


class DiscoveryNameAssignmentTests(unittest.TestCase):
    """OBL-HASHING-117: the same tracked files, the same names, everywhere."""

    def test_git_lists_the_manifests_in_byte_order(self):
        """The premise and the oracle: this is the order to follow."""
        for parents in (("Zebra", "apple"), ("a", "a-x")):
            with self.subTest(parents=parents):
                _names, tracked = _discovered(parents)
                self.assertEqual(tracked, sorted(tracked))

    def test_the_separator_boundary_is_ordered_by_the_path_bytes(self):
        """The first Git path receives the unsuffixed component name."""
        names, tracked = _discovered(("a", "a-x"))
        first = tracked[0].rsplit("/", 2)[0]
        self.assertEqual(names["svc"], f"{first}/svc")

    def test_the_separator_boundary_assigns_names_in_git_order(self):
        names, tracked = _discovered(("a", "a-x"))
        self.assertEqual(tracked[0], "a-x/svc/package.json")
        self.assertEqual(names, {"svc": "a-x/svc", "svc-2": "a/svc"})

    def test_the_case_boundary_is_ordered_by_the_path_bytes(self):
        """Name assignment is independent of host case-folding rules."""
        names, tracked = _discovered(("Zebra", "apple"))
        first = tracked[0].rsplit("/", 2)[0]
        self.assertEqual(names["svc"], f"{first}/svc")

    def test_path_object_order_can_differ_from_the_required_git_order(self):
        joined = ["a/svc/package.json", "a-x/svc/package.json"]
        self.assertEqual(sorted(joined), ["a-x/svc/package.json", "a/svc/package.json"])
        self.assertEqual(
            [path.as_posix() for path in sorted(Path(name) for name in joined)],
            ["a/svc/package.json", "a-x/svc/package.json"],
        )
        folds_case = os.path.normcase("A") != "A"
        cased = ["Zebra/svc/package.json", "apple/svc/package.json"]
        by_path = [path.as_posix() for path in sorted(Path(name) for name in cased)]
        self.assertEqual(by_path != sorted(cased), folds_case, by_path)

    def test_both_directories_are_discovered_either_way(self):
        """The contrast: what moves is which name goes where, not the set."""
        for parents in (("Zebra", "apple"), ("a", "a-x")):
            with self.subTest(parents=parents):
                names, _tracked = _discovered(parents)
                self.assertEqual(set(names), {"svc", "svc-2"})
                self.assertEqual(
                    sorted(names.values()), sorted(f"{p}/svc" for p in parents)
                )


#: Two distinct digests agreeing on the twelve characters the display keeps.
COLLIDING = ("0123456789ab" + "c" * 52, "0123456789ab" + "d" * 52)


def _real_diff_document() -> dict:
    """A diff document produced by the CLI, so the shape is not invented."""
    with Scenario() as scene:
        scene.component("svc", path="svc", boundary=["api"])
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.commit()
        assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
        (scene.root / "old.json").write_bytes(
            (scene.root / "boundary.lock.json").read_bytes()
        )
        scene.append_line("svc/api/v1.yaml", "change\n")
        scene.commit("edit")
        assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
        result = run_cli(
            scene.root, "diff", "old.json", "boundary.lock.json", "--format", "json"
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)


def _rendered(document: dict) -> list:
    stream = io.StringIO()
    with redirect_stdout(stream):
        output.print_diff(document)
    return stream.getvalue().splitlines()


#: One row of the `why` fingerprint table: the facet, the two shortened
#: digests either side of the arrow, and the marker that closes the line.
_FINGERPRINT_ROW = re.compile(
    r"^  (?P<facet>\S+) +(?P<left>\S+)  ->  (?P<right>\S+)  \((?P<note>[^)]+)\)$"
)


def _fingerprint_rows(lines: list) -> dict:
    """The rows of the `why` fingerprint table, keyed by facet.

    Reading starts at the table's own heading and stops at the first line that
    is not a row, so nothing else the command prints can be mistaken for one.
    """
    rows: dict = {}
    for line in lines[lines.index("Fingerprint changes:") + 1:]:
        match = _FINGERPRINT_ROW.match(line)
        if match is None:
            break
        rows[match.group("facet")] = match.groupdict()
    return rows


@contextmanager
def _drifted_component(edited: str):
    """A component that has drifted from its lock, with the old lock in place.

    The component declares `api` as its boundary and keeps a second directory
    outside it, so the caller chooses which facets move. Editing the boundary
    file drifts `exact` and `boundary` together; editing the file outside the
    boundary drifts `exact` alone and leaves `boundary` holding a real digest
    that both sides of its arrow are then supposed to show.
    """
    with Scenario() as scene:
        scene.component("svc", path="svc", boundary=["api"])
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.file("svc/impl/code.py", "value = 1\n")
        scene.commit()
        assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
        scene.append_line(edited, "change")
        scene.commit("edit")
        yield scene


def _why_renderings(edited: str) -> tuple:
    """The `why` fingerprint table beside the JSON view of the same run.

    The JSON view carries `changes`, which is the map the table is built from:
    one entry per drifted facet, holding the two digests under the names
    `locked` and `current`. That makes it the oracle for which value belongs
    on which side of the arrow. Both views come out of the CLI, so the table
    is exercised on the result it really receives rather than an invented one.
    """
    with _drifted_component(edited) as scene:
        result = run_cli(scene.root, "why", "svc")
        assert result.returncode == 1, result.stderr
        document = cli_json(scene.root, "why", "svc", "--format", "json")
        return document, _fingerprint_rows(result.stdout.splitlines())


def _why_table_with_colliding_digests() -> dict:
    """The same table, with the colliding pair substituted for every change.

    `why_component` runs in this interpreter so the two fingerprint maps can
    be replaced after the analysis and before the rendering. That is the trick
    the diff half of this file already plays on the diff document: a real run,
    with nothing changed but the digests.
    """
    with _drifted_component("svc/api/v1.yaml") as scene:
        config = json.loads(
            (scene.root / "boundary.config.json").read_text(encoding="utf-8")
        )
        lockfile = json.loads(
            (scene.root / "boundary.lock.json").read_text(encoding="utf-8")
        )
        analyze = output.analyze_component_drift

        def substituted(*args, **kwargs):
            result = analyze(*args, **kwargs)
            for facet in result["changes"]:
                result["locked_fps"][facet], result["current_fps"][facet] = COLLIDING
            return result

        stream = io.StringIO()
        with mock.patch.object(output, "analyze_component_drift", substituted):
            with redirect_stdout(stream):
                assert output.why_component(config, lockfile, scene.root, "svc") == 1
        return _fingerprint_rows(stream.getvalue().splitlines())


class ShortenedDigestDisplayTests(unittest.TestCase):
    """OBL-HASHING-019: an arrow must not point from a value to itself."""

    def test_a_real_change_renders_two_different_values(self):
        """The premise: ordinary digests do not collide in twelve characters."""
        lines = [line for line in _rendered(_real_diff_document()) if "->" in line]
        self.assertTrue(lines)
        for line in lines:
            with self.subTest(line=line):
                before, after = line.split(" -> ")
                self.assertNotEqual(before.split(": ")[-1], after)

    def test_the_two_digests_collide_when_shortened(self):
        """The premise for the divergence: distinct, and displayed alike."""
        left, right = COLLIDING
        self.assertNotEqual(left, right)
        self.assertEqual(_short(left), _short(right))

    def test_a_colliding_pair_is_not_rendered_as_an_identity(self):
        """A shared short prefix expands to distinguish the full digests."""
        document = _real_diff_document()
        for values in document["components"]["changed"][0]["changed_facets"].values():
            values["old"], values["new"] = COLLIDING
        for line in [line for line in _rendered(document) if "->" in line]:
            with self.subTest(line=line):
                before, after = line.split(" -> ")
                self.assertNotEqual(before.split(": ")[-1], after)

    def test_a_colliding_pair_expands_to_the_full_digests(self):
        document = _real_diff_document()
        for values in document["components"]["changed"][0]["changed_facets"].values():
            values["old"], values["new"] = COLLIDING
        lines = [line for line in _rendered(document) if "->" in line]
        self.assertTrue(lines)
        for line in lines:
            with self.subTest(line=line):
                self.assertIn(f"{COLLIDING[0]} -> {COLLIDING[1]}", line)

    def test_the_why_table_shows_the_locked_digest_on_the_left(self):
        """MUT-HASHING-410: each side of the arrow carries its own field.

        The obligation names two renderings, `print_diff` and the `why`
        fingerprint table, and until now the tests reached only the first of
        them. That left the why table free to print the current digest on both
        sides of its arrow, so every drifted facet would read as a value
        pointing at itself: the shape the obligation forbids, printed by the
        command a reviewer runs precisely to find out what moved. Nothing in
        the suite would have failed.

        The assertion binds each side to the field it is meant to show rather
        than merely requiring the two to differ, because a table that printed
        the pair the wrong way round would satisfy a difference check while
        telling the reviewer that the value in the lock is the one on disk.
        """
        document, rows = _why_renderings("svc/api/v1.yaml")
        self.assertEqual(sorted(document["changes"]), ["boundary", "exact"])
        for facet, values in document["changes"].items():
            with self.subTest(facet=facet):
                self.assertEqual(rows[facet]["left"], _short(values["locked"]))
                self.assertEqual(rows[facet]["right"], _short(values["current"]))
                self.assertTrue(rows[facet]["note"].startswith("changed, "))

    def test_the_drifted_facets_shorten_to_two_distinguishable_values(self):
        """The premise: this fixture can tell the two sides of the arrow apart.

        If the locked and current digests of a drifted facet shortened to the
        same twelve characters, then an assertion that the left field holds
        the locked value would pass whichever of the two the table printed,
        and the check above would hold vacuously. This states that they do not
        collide, that neither is absent, and that the table lists every facet
        rather than only the ones that moved.
        """
        document, rows = _why_renderings("svc/api/v1.yaml")
        self.assertEqual(sorted(rows), sorted(FACETS))
        self.assertTrue(document["changes"])
        for facet, values in document["changes"].items():
            with self.subTest(facet=facet):
                self.assertIsNotNone(values["locked"])
                self.assertIsNotNone(values["current"])
                self.assertNotEqual(
                    _short(values["locked"]), _short(values["current"])
                )

    def test_an_unchanged_facet_still_prints_its_digest_on_both_sides(self):
        """The contrast: an identity arrow is right when nothing moved.

        A check that rejected every arrow whose two sides matched would pass
        this file and still be wrong, because a facet that did not drift is
        supposed to show one digest twice. Editing a file outside the declared
        boundary drifts `exact` alone, so `boundary` keeps a real digest and
        prints it on both sides under the unchanged marker, while the drifted
        row beside it is still rendered the ordinary way.
        """
        document, rows = _why_renderings("svc/impl/code.py")
        self.assertEqual(sorted(document["changes"]), ["exact"])
        self.assertEqual(rows["boundary"]["note"], "unchanged")
        self.assertEqual(rows["boundary"]["left"], rows["boundary"]["right"])
        self.assertNotEqual(rows["boundary"]["left"], "none")
        changed = document["changes"]["exact"]
        self.assertEqual(rows["exact"]["left"], _short(changed["locked"]))
        self.assertEqual(rows["exact"]["right"], _short(changed["current"]))

    def test_the_why_table_expands_and_marks_a_colliding_pair_as_changed(self):
        rows = _why_table_with_colliding_digests()
        colliding = {
            facet: row
            for facet, row in rows.items()
            if row["left"] == COLLIDING[0]
        }
        self.assertEqual(sorted(colliding), ["boundary", "exact"])
        for facet, row in colliding.items():
            with self.subTest(facet=facet):
                self.assertEqual(row["left"], COLLIDING[0])
                self.assertEqual(row["right"], COLLIDING[1])
                self.assertIn(row["note"], ("changed, gating", "changed, non-gating"))


class PolicyDigestTests(unittest.TestCase):
    """OBL-HASHING-020: the policy payload moves with effective gating."""

    BASE = {
        "project": "p",
        "components": {
            "svc": {
                "path": "svc",
                "boundary": {"provider": "path-hash", "paths": ["api"]},
                "verify_facets": ["boundary"],
            },
            "leafy": {"path": "leafy", "boundary": {"provider": "leaf", "paths": []}},
        },
        "slices": {"s": {"components": ["svc"], "mode": "exact"}},
    }

    def _payload(self, edit=None) -> dict:
        config = copy.deepcopy(self.BASE)
        if edit is not None:
            edit(config)
        return facet_policy_payload(config, None)

    def test_a_gating_change_moves_the_payload(self):
        gating = {
            "component verify_facets": lambda cfg:
                cfg["components"]["svc"].__setitem__("verify_facets", ["exact"]),
            "defaults verify_facets": lambda cfg:
                cfg.__setitem__("defaults", {"verify_facets": ["exact"]}),
            "slice mode": lambda cfg:
                cfg["slices"]["s"].__setitem__("mode", "boundary"),
            "slice gated flag": lambda cfg:
                cfg["slices"]["s"].__setitem__("components", ["leafy"]),
        }
        unchanged = self._payload()
        for label, edit in gating.items():
            with self.subTest(edit=label):
                self.assertNotEqual(unchanged, self._payload(edit))

    def test_the_gated_flag_really_does_flip(self):
        """The premise for the fourth case, which is otherwise a membership edit."""
        before = self._payload()["slices"]["s"]
        after = self._payload(
            lambda cfg: cfg["slices"]["s"].__setitem__("components", ["leafy"])
        )["slices"]["s"]
        self.assertFalse(before["gated"])
        self.assertTrue(after["gated"])

    def test_a_cosmetic_change_does_not_move_the_payload(self):
        cosmetic = {
            "project rename": lambda cfg: cfg.__setitem__("project", "renamed"),
            "slice description": lambda cfg:
                cfg["slices"]["s"].__setitem__("description", "a note"),
            "boundary note": lambda cfg:
                cfg["components"]["svc"]["boundary"].__setitem__("note", "a note"),
            "component key order": lambda cfg:
                cfg.__setitem__("components", dict(reversed(list(cfg["components"].items())))),
        }
        unchanged = self._payload()
        for label, edit in cosmetic.items():
            with self.subTest(edit=label):
                self.assertEqual(unchanged, self._payload(edit))

    def test_a_membership_change_that_flips_nothing_is_not_recorded(self):
        """A limit worth writing down: the payload keeps mode and gated only.

        A slice that gains a member whose gating already matched leaves the
        payload identical, so a baseline captured before the edit stays valid.
        That follows from the obligation's own wording - it names the gated
        flag flipping, not membership - but it is the kind of thing a reader
        would otherwise have to derive.
        """
        both = self._payload(
            lambda cfg: cfg["slices"]["s"].__setitem__("components", ["svc", "leafy"])
        )
        self.assertEqual(both["slices"], {"s": {"mode": "exact", "gated": True}})
        self.assertNotIn("components", both["slices"]["s"])


if __name__ == "__main__":
    unittest.main()
