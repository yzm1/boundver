"""The last four config obligations with nothing asserting them.

Unicode normalization, slice evaluation under a component filter, baseline
violation identity, and the facet spelling a baseline was captured with. Each
needs a repository and nothing else.

Covers OBL-CONFIG-002, OBL-CONFIG-004, OBL-CONFIG-006 and OBL-CONFIG-008.
"""

from __future__ import annotations

import collections
import json
import unicodedata
import unittest

from boundver._utils import ConfigError, _is_glob

from tests._parity import run_cli
from tests._scenarios import Scenario

#: The same name in both normal forms: one code point versus e plus a
#: combining acute. Byte-different, visually identical.
NFC_NAME = unicodedata.normalize("NFC", "café.yaml")
NFD_NAME = unicodedata.normalize("NFD", "café.yaml")


class UnicodeNormalizationTests(unittest.TestCase):
    """OBL-CONFIG-002: one deterministic outcome, whatever the host.

    Selection runs over the paths Git recorded, not over the filesystem, so
    the answer is fixed by what `git add` stored rather than by whether the
    filesystem normalizes. That is what makes it the same on every platform.
    """

    def _selects(self, on_disk: str, declared: str) -> bool:
        with Scenario() as scene:
            scene.component("svc", path="svc", boundary=[f"api/{declared}"])
            scene.file(f"svc/api/{on_disk}", "openapi: 3.1.0\n")
            scene.commit()
            try:
                scene.digest("svc", "boundary")
            except ConfigError as error:
                self.assertIn("matched no tracked files", str(error))
                return False
            return True

    def test_the_two_forms_really_are_different_byte_strings(self):
        self.assertNotEqual(NFC_NAME, NFD_NAME)
        self.assertEqual(len(NFC_NAME) + 1, len(NFD_NAME))
        self.assertEqual(
            unicodedata.normalize("NFC", NFD_NAME),
            NFC_NAME,
        )

    def test_a_declaration_selects_only_its_own_form(self):
        self.assertTrue(self._selects(NFC_NAME, NFC_NAME))
        self.assertTrue(self._selects(NFD_NAME, NFD_NAME))

    def test_the_other_form_fails_with_the_unmatched_error(self):
        """Not silently, and not by selecting something the user did not name."""
        self.assertFalse(self._selects(NFD_NAME, NFC_NAME))
        self.assertFalse(self._selects(NFC_NAME, NFD_NAME))

    def _selects_through_a_glob(self, on_disk: str, declared: str) -> bool:
        """The same question asked with a pattern instead of a literal path.

        A declaration that carries no metacharacter is resolved by asking Git
        for the tracked paths underneath it. A declaration that carries one is
        resolved by running the segment matcher over every tracked file in the
        component, so the accented name is compared by a different piece of
        code. Both routes have to give the same answer.
        """
        with Scenario() as scene:
            scene.component("svc", path="svc", boundary=[f"*/{declared}"])
            scene.file(f"svc/api/{on_disk}", "openapi: 3.1.0\n")
            scene.commit()
            try:
                scene.digest("svc", "boundary")
            except ConfigError as error:
                self.assertIn("matched no tracked files", str(error))
                return False
            return True

    def test_the_two_declarations_take_different_matching_routes(self):
        """The premise for the glob tests below, asserted rather than assumed.

        The declaration the tests above use is a plain path, so boundary
        selection resolves it by listing the tracked files under it and never
        reaches the segment matcher. Prefixing a star makes it a pattern, and
        the accented segment is then compared character by character by the
        matcher instead. Without this assertion the glob tests would read as
        duplicates of the literal ones, and someone could delete them on that
        belief.
        """
        self.assertFalse(_is_glob(f"api/{NFC_NAME}"))
        self.assertFalse(_is_glob(f"api/{NFD_NAME}"))
        self.assertTrue(_is_glob(f"*/{NFC_NAME}"))
        self.assertTrue(_is_glob(f"*/{NFD_NAME}"))

    def test_a_glob_declaration_selects_its_own_form(self):
        """The contrast: the pattern route still accepts the ordinary case.

        A matcher that refused every accented name would satisfy the refusals
        below and be useless, so assert first that a pattern written in the
        form the file is stored in does select it.
        """
        self.assertTrue(self._selects_through_a_glob(NFC_NAME, NFC_NAME))
        self.assertTrue(self._selects_through_a_glob(NFD_NAME, NFD_NAME))

    def test_a_glob_declaration_does_not_fold_the_two_forms_together(self):
        """MUT-CONFIG-402: the segment matcher must not normalize either side.

        The literal segments of a pattern are compared by the bounded equality
        helper in boundver._utils, which none of the tests above reach because
        their declarations are plain paths. Normalizing both sides to NFC
        inside that helper leaves every other assertion in this class green,
        while a declaration quietly begins selecting a file whose name the user
        did not write. Selecting a different file is exactly the outcome
        OBL-CONFIG-002 rules out, so the pattern route has to refuse the other
        form for the same reason the literal route does.
        """
        self.assertFalse(self._selects_through_a_glob(NFD_NAME, NFC_NAME))
        self.assertFalse(self._selects_through_a_glob(NFC_NAME, NFD_NAME))


class SliceGatingUnderComponentFilterTests(unittest.TestCase):
    """OBL-CONFIG-004: a filter must not hide a slice that contains the filtered component."""

    def _repository(self, *, drift: bool = True) -> Scenario:
        scene = Scenario()
        scene.component("api", path="api", boundary=["spec"], consumers=["sdk"])
        scene.component("sdk", path="sdk", provider="leaf", consumers=["app"])
        scene.component("app", path="app", provider="leaf")
        scene.slice("closure", mode="exact", closure_of="api")
        scene.slice("explicit", mode="exact", components=["api", "app"])
        scene.file("api/spec/v1.yaml", "openapi: 3.1.0\n")
        scene.file("sdk/index.ts", "export const x = 1;\n")
        scene.file("app/main.ts", "const y = 2;\n")
        scene.commit()
        run_cli(scene.root, "generate", "--source", "head")
        scene.commit("lock")
        if drift:
            scene.file("app/main.ts", "const y = 3;\n")
            scene.commit("drift in app only")
        return scene

    @staticmethod
    def _slice_mismatches(result) -> set:
        """The names of the slices reported, from the indented report lines."""
        text = (result.stdout or "") + (result.stderr or "")
        found = set()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("SLICE MISMATCH "):
                found.add(stripped.split()[2].split(".")[0])
        return found

    def test_the_closure_slice_reaches_the_drifted_component(self):
        """The premise: `closure` contains app only transitively, through sdk."""
        with self._repository() as scene:
            members = scene.generate()["slices"]["closure"]["components"]
            self.assertEqual(members, ["api", "app", "sdk"])

    def test_a_filter_reports_every_slice_that_contains_the_component(self):
        """Membership decides, not whether the component itself drifted.

        `sdk` is a member of `closure` and not of `explicit`, so filtering to
        it must still report `closure` even though the drift is in `app`. That
        is the point of the obligation: a filter narrows which components are
        compared, not which slices are evaluated.
        """
        with self._repository() as scene:
            memberships = {
                name: set(entry["components"])
                for name, entry in scene.generate()["slices"].items()
            }
            unfiltered = run_cli(scene.root, "verify", "--source", "head")
            self.assertEqual(
                self._slice_mismatches(unfiltered), {"closure", "explicit"}
            )

            for component in ("api", "sdk", "app"):
                with self.subTest(components=component):
                    expected = {
                        name for name, members in memberships.items()
                        if component in members
                    }
                    filtered = run_cli(
                        scene.root, "verify", "--source", "head",
                        "--components", component,
                    )
                    self.assertEqual(self._slice_mismatches(filtered), expected)
                    self.assertEqual(filtered.returncode, unfiltered.returncode)

    def test_filtering_to_an_uninvolved_component_still_reports_its_slice(self):
        """sdk did not change, and its slice is still evaluated and reported."""
        with self._repository() as scene:
            result = run_cli(
                scene.root, "verify", "--source", "head", "--components", "sdk"
            )
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertEqual(self._slice_mismatches(result), {"closure"})

    @staticmethod
    def _slice_digests(result) -> dict:
        """The whole report line for each slice, not only the slice name.

        `_slice_mismatches` keeps the name, which answers which slices were
        evaluated and says nothing about what was compared inside them. Parse
        `SLICE MISMATCH <name>.<facet>: lockfile=<a> current=<b>` into
        `{name: (facet, a, b)}` so a test can ask the second question too.
        """
        text = (result.stdout or "") + (result.stderr or "")
        found = {}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("SLICE MISMATCH "):
                continue
            subject, _, remainder = stripped[len("SLICE MISMATCH "):].partition(":")
            name, _, facet = subject.partition(".")
            fields = dict(
                field.split("=", 1) for field in remainder.split() if "=" in field
            )
            found[name] = (facet, fields["lockfile"], fields["current"])
        return found

    def test_every_reported_slice_names_two_digests_that_differ(self):
        """The premise: there really is a comparison behind each reported slice.

        The test below compares the digest a filtered run computed against the
        digest the unfiltered run computed for the same slice. That comparison
        would hold vacuously if the report carried no digests at all, or if
        every slice reported one constant on both sides, so pin both here: each
        reported slice names a lockfile digest and a current digest, and the
        two are different.
        """
        with self._repository() as scene:
            unfiltered = run_cli(scene.root, "verify", "--source", "head")
            reported = self._slice_digests(unfiltered)
            self.assertEqual(set(reported), {"closure", "explicit"})
            for name, (facet, recorded, current) in reported.items():
                with self.subTest(slice_name=name):
                    self.assertEqual(facet, "exact")
                    self.assertNotEqual(recorded, current)

    def test_a_filter_computes_the_slice_digest_from_the_whole_membership(self):
        """MUT-CONFIG-403: a filter narrows the comparison, not the aggregate.

        The obligation has two clauses and the tests above assert only the
        first. Reporting the right slices is not enough, because the member
        entries a slice aggregates must still be computed for every resolved
        member rather than only for the components the filter selected.
        Restricting that pre-computation to the filtered components leaves the
        reported slice names and the exit code exactly as they were and quietly
        changes the current digest, so a scoped run would report a fingerprint
        no unfiltered run ever produces and a developer comparing the two
        outputs would be reading a number that means nothing. Assert the
        digests themselves agree, slice by slice.
        """
        with self._repository() as scene:
            unfiltered = self._slice_digests(
                run_cli(scene.root, "verify", "--source", "head")
            )
            for component in ("api", "sdk", "app"):
                with self.subTest(components=component):
                    filtered = self._slice_digests(
                        run_cli(
                            scene.root, "verify", "--source", "head",
                            "--components", component,
                        )
                    )
                    self.assertTrue(filtered)
                    for name, report in filtered.items():
                        self.assertIn(name, unfiltered)
                        self.assertEqual(report, unfiltered[name])

    def test_a_filter_reports_no_slice_when_nothing_drifted(self):
        """The contrast: reporting every containing slice is not reporting always.

        Every assertion above reads a repository whose app component has
        drifted, so an implementation that named each declared slice on every
        run would satisfy them. Take the drift away and the same filtered
        commands have to accept the tree and report nothing.
        """
        with self._repository(drift=False) as scene:
            for component in ("api", "sdk", "app"):
                with self.subTest(components=component):
                    result = run_cli(
                        scene.root, "verify", "--source", "head",
                        "--components", component,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertEqual(self._slice_digests(result), {})


class BaselineViolationIdentityTests(unittest.TestCase):
    """OBL-CONFIG-006: a truncated display name must not become an identity."""

    SHARED = "c" * 497

    def _baseline_and_mismatches(self) -> tuple:
        """The written baseline, and how many mismatches verify reported.

        Both numbers, because the interesting question is whether they agree.
        """
        first = self.SHARED + "A" * 103
        second = self.SHARED + "B" * 103
        with Scenario() as scene:
            for name, tag in ((first, "A"), (second, "B")):
                scene.component(name, path=f"svc{tag}", boundary=["api"])
                scene.file(f"svc{tag}/api/v1.yaml", "openapi: 3.1.0\n")
            scene.commit()
            run_cli(scene.root, "generate", "--source", "head")
            scene.commit("lock")
            for tag in ("A", "B"):
                scene.append_line(f"svc{tag}/api/v1.yaml", f"drift {tag}\n")
            scene.commit("drift")
            plain = run_cli(scene.root, "verify", "--source", "head")
            text = (plain.stdout or "") + (plain.stderr or "")
            reported = sum(
                1 for line in text.splitlines() if "MISMATCH" in line
            )
            written = run_cli(
                scene.root, "verify", "--source", "head",
                "--write-baseline", "bv.baseline.json",
            )
            self.assertEqual(written.returncode, 0, written.stderr)
            document = json.loads(
                (scene.root / "bv.baseline.json").read_text(encoding="utf-8")
            )
            return document, reported

    def _baseline(self) -> dict:
        first = self.SHARED + "A" * 103
        second = self.SHARED + "B" * 103
        with Scenario() as scene:
            for name, tag in ((first, "A"), (second, "B")):
                scene.component(name, path=f"svc{tag}", boundary=["api"])
                scene.file(f"svc{tag}/api/v1.yaml", "openapi: 3.1.0\n")
            scene.commit()
            run_cli(scene.root, "generate", "--source", "head")
            scene.commit("lock")
            for tag in ("A", "B"):
                scene.append_line(f"svc{tag}/api/v1.yaml", "drift: true")
            scene.commit("drift")
            result = run_cli(
                scene.root, "verify", "--source", "head",
                "--write-baseline", "bv.baseline.json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(
                (scene.root / "bv.baseline.json").read_text(encoding="utf-8")
            )

    def test_long_display_names_have_distinct_identity_suffixes(self):
        document = self._baseline()
        subjects = collections.Counter(v["subject"] for v in document["violations"])
        self.assertEqual(len(subjects), 2)
        subject, count = next(iter(subjects.items()))
        self.assertEqual(count, 2)
        self.assertEqual(len(subject), 500)
        self.assertRegex(subject, r"\.\.\.#[0-9a-f]{16}$")

    def test_every_reported_mismatch_gets_its_own_baseline_entry(self):
        """Long component labels retain a stable distinguishing suffix.

        Every mismatch must produce one baseline entry even when names share
        more prefix text than the diagnostic ceiling can display.
        """
        document, reported = self._baseline_and_mismatches()
        self.assertEqual(len(document["violations"]), reported)

    def test_the_identity_set_is_one_entry_per_component_and_facet(self):
        document, reported = self._baseline_and_mismatches()
        violations = document["violations"]
        self.assertEqual(reported, 4)
        self.assertEqual(len(violations), 4)
        self.assertEqual(len({v["id"] for v in violations}), 4)
        self.assertEqual({v["facet"] for v in violations}, {"exact", "boundary"})
        self.assertEqual(len({v["subject"] for v in violations}), 2)


class BaselineFacetSpellingTests(unittest.TestCase):
    """OBL-CONFIG-008: how the scope was spelled is part of the baseline."""

    def _with_baseline(self, *capture_flags) -> Scenario:
        scene = Scenario()
        scene.component("svc", path="svc", boundary=["api"])
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.config["defaults"] = {"verify_facets": ["exact", "boundary"]}
        scene.commit()
        run_cli(scene.root, "generate", "--source", "head")
        scene.commit("lock")
        scene.append_line("svc/api/v1.yaml", "drift: true")
        scene.commit("drift")
        result = run_cli(
            scene.root, "verify", "--source", "head", *capture_flags,
            "--write-baseline", "bv.baseline.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        scene.git("add", "--all")
        scene.git("commit", "-m", "baseline")
        return scene

    def test_a_baseline_captured_with_facets_is_refused_without_them(self):
        flags = ("--facets", "exact", "--facets", "boundary")
        with self._with_baseline(*flags) as scene:
            result = run_cli(
                scene.root, "verify", "--source", "head",
                "--baseline", "bv.baseline.json",
            )
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertIn("facets does not match", result.stderr)

    def test_a_baseline_captured_without_facets_is_refused_with_them(self):
        with self._with_baseline() as scene:
            result = run_cli(
                scene.root, "verify", "--source", "head",
                "--facets", "exact", "--facets", "boundary",
                "--baseline", "bv.baseline.json",
            )
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertIn("facets does not match", result.stderr)

    def test_the_same_spelling_is_accepted(self):
        """The contrast, without which the two refusals prove nothing."""
        flags = ("--facets", "exact", "--facets", "boundary")
        with self._with_baseline(*flags) as scene:
            result = run_cli(
                scene.root, "verify", "--source", "head", *flags,
                "--baseline", "bv.baseline.json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def _captured_facets(self, *capture_flags):
        """The `facets` field the capture wrote, read back from the baseline."""
        with self._with_baseline(*capture_flags) as scene:
            document = json.loads(
                (scene.root / "bv.baseline.json").read_text(encoding="utf-8")
            )
            return document["facets"]

    def test_a_repeated_facets_flag_records_only_its_last_occurrence(self):
        """The premise behind the spelling the tests above use.

        `--facets` takes one comma-separated string rather than accumulating,
        so repeating the flag keeps the last occurrence and the pair above asks
        for `boundary` alone. Reading the flags as a two-facet request would
        make the assertion below look wrong, so record here what the flag
        actually means.
        """
        pair = ("--facets", "exact", "--facets", "boundary")
        self.assertEqual(self._captured_facets(*pair), ["boundary"])
        self.assertEqual(
            self._captured_facets("--facets", "boundary,exact"),
            ["boundary", "exact"],
        )

    def test_the_baseline_records_which_run_named_its_facets(self):
        """The recorded value itself, not only that two captures disagree.

        The three tests above are symmetric: two refusals and one acceptance
        stay true whenever the two contexts differ, so they hold just as well
        when the recording is inverted and an explicit `--facets` stores null
        while an absent one stores the resolved list. Applying that inversion
        to the assignment of the explicit facet list in core.verify by hand
        leaves all three green, and it also feeds the wrong argument to the
        facet policy payload, so every run's policy digest moves. An equality
        on the field is what an inversion cannot satisfy: a capture that named
        no facets has to record null, and a capture that named them has to
        record exactly what it named. No mutant id covers this one.
        """
        self.assertIsNone(self._captured_facets())
        self.assertEqual(
            self._captured_facets("--facets", "boundary,exact"),
            ["boundary", "exact"],
        )


if __name__ == "__main__":
    unittest.main()
