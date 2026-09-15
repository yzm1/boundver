"""A pre-scan that may only over-approximate, and a name that is not text.

The strict JSON pre-scan exists to refuse a provably oversized document before
json.loads allocates it. Being cheap, it is allowed to be conservative: it may
accept something the exact validator will refuse. What it must never do is the
reverse, so the property to assert is one-directional.

The selector's edge is the mirror image. A Git filename need not be valid
UTF-8, and Python carries those bytes as surrogates in the DC80-DCFF range. A
surrogate outside that range is not a filename at all, and answering "no match"
to it is the wrong kind of quiet.

Covers OBL-CROSSCUTTING-006, OBL-FACETS-012, OBL-FACETS-013 and
OBL-BASELINE-005.
"""

from __future__ import annotations

import json
import os
import stat
import unittest

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from boundver._structured_data import _reject_excessive_json_tokens
from boundver._utils import (
    MAX_JSON_TREE_DEPTH,
    GuardrailError,
    _match_path_glob,
    _validated_path_glob_candidate,
)

from tests._parity import run_cli
from tests._scenarios import Scenario

PROFILE = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

#: A byte Git can put in a filename that is not valid UTF-8, as Python
#: carries it: the surrogateescape range.
SURROGATE_ESCAPED = "\udcff"

#: A lone surrogate that is not an escaped byte, so not a filename.
LONE_SURROGATE = "\ud800"


def _nested(levels: int) -> str:
    return "[" * levels + "1" + "]" * levels


def _scanner_verdict(text: str) -> str:
    try:
        _reject_excessive_json_tokens(text)
    except Exception:
        return "rejected"
    return "accepted"


class PreScanDirectionTests(unittest.TestCase):
    """OBL-CROSSCUTTING-006: conservative in one direction only."""

    def test_the_scanner_accepts_the_documented_depth(self):
        for levels in (1, MAX_JSON_TREE_DEPTH - 1, MAX_JSON_TREE_DEPTH):
            with self.subTest(levels=levels):
                self.assertEqual(_scanner_verdict(_nested(levels)), "accepted")

    def test_the_scanner_rejects_beyond_one_past_the_ceiling(self):
        self.assertEqual(
            _scanner_verdict(_nested(MAX_JSON_TREE_DEPTH + 2)), "rejected"
        )

    def test_the_over_approximation_is_exactly_one_level(self):
        """Pin where the scanner is looser than the exact validator."""
        self.assertEqual(
            _scanner_verdict(_nested(MAX_JSON_TREE_DEPTH + 1)), "accepted"
        )
        self.assertEqual(
            _scanner_verdict(_nested(MAX_JSON_TREE_DEPTH + 2)), "rejected"
        )

    def test_a_string_may_hold_any_bracket_or_escape(self):
        """The state machine the gap said was unexercised."""
        for text in (
            '{"a": "]]]]]"}',
            '{"a": "[[[[["}',
            '{"a": "{}{}{}"}',
            '{"a": "\\\\"}',
            '["[", "{", "\\\\"]',
            '{"a\\"b": 1}',
        ):
            with self.subTest(document=text):
                self.assertIsNotNone(json.loads(text))
                self.assertEqual(_scanner_verdict(text), "accepted")

    @PROFILE
    @given(
        value=st.recursive(
            st.one_of(
                st.none(),
                st.booleans(),
                st.integers(min_value=-1000, max_value=1000),
                st.text(alphabet='ab"\\[]{},: ', max_size=6),
            ),
            lambda children: st.one_of(
                st.lists(children, max_size=3),
                st.dictionaries(
                    st.text(alphabet='ab"\\[]{}', max_size=4), children, max_size=3
                ),
            ),
            max_leaves=10,
        )
    )
    def test_the_scanner_never_rejects_a_small_valid_document(self, value):
        """The false-positive half, over generated documents.

        Every document here is far inside both ceilings, so a rejection could
        only come from the scanner misreading a string.
        """
        text = json.dumps(value)
        self.assertEqual(json.loads(text), value)
        self.assertEqual(_scanner_verdict(text), "accepted")


class NonUtf8CandidateTests(unittest.TestCase):
    """OBL-FACETS-012: a name that is bytes, and one that is nothing."""

    def test_an_escaped_byte_name_is_matched(self):
        for pattern in ("*", "**"):
            with self.subTest(pattern=pattern):
                self.assertTrue(_match_path_glob(f"{SURROGATE_ESCAPED}.bin", pattern))

    def test_an_escaped_byte_name_survives_the_round_trip(self):
        """The label must encode back to the bytes Git had."""
        name = f"{SURROGATE_ESCAPED}.bin"
        segments = _validated_path_glob_candidate(name)
        self.assertEqual(segments, (name,))
        self.assertEqual(
            name.encode("utf-8", errors="surrogateescape"), b"\xff.bin"
        )

    def test_a_nested_escaped_byte_name_is_matched_by_the_deep_pattern(self):
        self.assertTrue(_match_path_glob(f"a/{SURROGATE_ESCAPED}.bin", "**"))
        self.assertFalse(_match_path_glob(f"a/{SURROGATE_ESCAPED}.bin", "*"))

    def test_a_lone_surrogate_fails_closed(self):
        with self.assertRaises(GuardrailError):
            _match_path_glob(f"{LONE_SURROGATE}.bin", "*")

    def test_invalid_unicode_never_becomes_a_quiet_non_match(self):
        for pattern in ("*", "**"):
            with self.subTest(pattern=pattern):
                self.assertTrue(_match_path_glob(f"{SURROGATE_ESCAPED}.bin", pattern))
                with self.assertRaises(GuardrailError):
                    _match_path_glob(f"{LONE_SURROGATE}.bin", pattern)
        with self.assertRaises(GuardrailError):
            _validated_path_glob_candidate(f"{LONE_SURROGATE}.bin")


class EndpointEquivalenceTests(unittest.TestCase):
    """OBL-FACETS-013: two spellings of one range, one answer."""

    def _range(self):
        scene = Scenario()
        scene.component("svc", path="svc", boundary=["api"])
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.commit()
        assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
        scene.commit("lock")
        base = scene.head()
        scene.append_line("svc/api/v1.yaml", "change\n")
        scene.commit("edit")
        assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
        scene.commit("relock")
        return scene, base, scene.head()

    def test_the_positional_and_explicit_forms_agree_exactly(self):
        scene, base, target = self._range()
        try:
            positional = run_cli(
                scene.root, "review", f"{base}..{target}", "--format", "json"
            )
            explicit = run_cli(
                scene.root, "review", "--base", base, "--target", target,
                "--format", "json",
            )
            self.assertEqual(positional.returncode, explicit.returncode)
            self.assertEqual(positional.stdout, explicit.stdout)
        finally:
            scene.close()

    def test_a_symbolic_ref_resolves_to_the_same_commit_it_names(self):
        """Each endpoint is recorded as a resolved commit, once."""
        scene, base, target = self._range()
        try:
            branch = scene.git("rev-parse", "--abbrev-ref", "HEAD")
            document = json.loads(run_cli(
                scene.root, "review", "--base", base, "--target", branch,
                "--format", "json",
            ).stdout)
            endpoints = document["endpoints"]
            self.assertEqual(endpoints["base"]["commit"], base)
            self.assertEqual(endpoints["target"]["commit"], target)
            self.assertEqual(endpoints["target"]["requested_ref"], branch)
            self.assertEqual(endpoints["target"]["requested_commit"], target)
        finally:
            scene.close()

    def test_the_flag_is_recorded_and_the_base_is_still_disclosed(self):
        """On a linear history the two bases coincide; both are still stated.

        The branched case, where they differ and the effective base carries
        its marker, is asserted in test_plan_selection.py.
        """
        scene, base, target = self._range()
        try:
            answers = {}
            for label, extra in (("direct", []), ("merge-base", ["--merge-base"])):
                answers[label] = json.loads(run_cli(
                    scene.root, "review", "--base", base, "--target", target,
                    "--format", "json", *extra,
                ).stdout)
            self.assertFalse(answers["direct"]["request"]["merge_base"])
            self.assertTrue(answers["merge-base"]["request"]["merge_base"])
            for label, document in answers.items():
                with self.subTest(run=label):
                    endpoint = document["endpoints"]["base"]
                    self.assertEqual(endpoint["requested_ref"], base)
                    self.assertEqual(endpoint["requested_commit"], base)
                    self.assertEqual(endpoint["commit"], base)
        finally:
            scene.close()


on_posix = unittest.skipIf(
    os.name == "nt", "Windows does not carry POSIX permission bits"
)


class BaselineModeTests(unittest.TestCase):
    """OBL-BASELINE-005: the mode a baseline lands with, and keeps."""

    def _locked(self):
        scene = Scenario()
        scene.component("svc", path="svc", provider="leaf")
        scene.file("svc/main.py", "x\n")
        scene.commit()
        assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
        scene.commit("lock")
        return scene

    def test_a_baseline_is_written_and_leaves_no_sidecar(self):
        """Platform-neutral: whatever the mode, no claim file survives."""
        scene = self._locked()
        try:
            result = run_cli(
                scene.root, "verify", "--source", "head", "--write-baseline", "b.json"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((scene.root / "b.json").is_file())
            self.assertEqual(
                [entry.name for entry in scene.root.iterdir() if ".claim" in entry.name],
                [],
            )
        finally:
            scene.close()

    @on_posix
    def test_a_created_baseline_lands_at_the_documented_mode(self):
        scene = self._locked()
        try:
            run_cli(scene.root, "verify", "--source", "head", "--write-baseline", "b.json")
            mode = stat.S_IMODE((scene.root / "b.json").stat().st_mode)
            self.assertEqual(mode, 0o600, oct(mode))
        finally:
            scene.close()

    @on_posix
    def test_an_update_preserves_the_existing_mode(self):
        scene = self._locked()
        try:
            run_cli(scene.root, "verify", "--source", "head", "--write-baseline", "b.json")
            scene.git("add", "--all")
            scene.git("commit", "-m", "baseline")
            target = scene.root / "b.json"
            os.chmod(target, 0o400)
            result = run_cli(
                scene.root, "verify", "--source", "head", "--update-baseline", "b.json"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o400)
        finally:
            scene.close()


if __name__ == "__main__":
    unittest.main()
