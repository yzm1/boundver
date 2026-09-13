"""What the config digest folds together, and what it insists is different.

`config_digest` is a digest of a projection of the configuration: the parts
that change a generated lock, with presentation stripped out. Two things can
go wrong. It can fold together configs that mean different things, in which
case a real change goes unnoticed. Or it can separate configs that mean the
same thing, in which case a no-op edit reports drift and forces a
regeneration. The second is the failure mode this file mostly finds.

Covers OBL-HASHING-003, 004, 005, 009, 015, 016, 017 and 018.
"""

from __future__ import annotations

import copy
import json
import unittest

from boundver._lockfile import _normalized_semantic_path, semantic_config_digest
from boundver._utils import _normalize_declared_path

from tests._parity import run_cli
from tests._scenarios import Scenario

DRIFT = 1
COULD_NOT_CHECK = 2

BASE = {
    "project": "p",
    "components": {
        "svc": {
            "path": "svc",
            "boundary": {"provider": "path-hash", "paths": ["api"]},
        }
    },
    "slices": {"s": {"components": ["svc"]}},
}

#: Spellings posixpath.normpath folds, paired with what it folds them onto.
FOLDED = (("a//b", "a/b"), ("a/./b", "a/b"), ("a/../b", "b"))


def _variant(**mutate) -> dict:
    """BASE with dotted-path edits; a value of ... removes the key."""
    config = copy.deepcopy(BASE)
    for path, value in mutate.items():
        target = config
        keys = path.split("__")
        for key in keys[:-1]:
            target = target.setdefault(key, {})
        if value is ...:
            target.pop(keys[-1], None)
        else:
            target[keys[-1]] = value
    return config


def _same_digest(left: dict, right: dict) -> bool:
    return semantic_config_digest(left) == semantic_config_digest(right)


#: Pairs that must digest alike: one spelling omits what the other states.
EQUIVALENT_PAIRS = {
    "defaults absent vs empty": (_variant(), _variant(defaults={})),
    "compat_mode absent vs major": (
        _variant(defaults={}), _variant(defaults={"compat_mode": "major"})),
    "defaults verify_facets absent vs null": (
        _variant(defaults={}), _variant(defaults={"verify_facets": None})),
    "component verify_facets absent vs null": (
        _variant(), _variant(components__svc__verify_facets=None)),
    "component behavior absent vs null": (
        _variant(), _variant(components__svc__behavior=None)),
    "component version_source absent vs null": (
        _variant(), _variant(components__svc__version_source=None)),
    "slice description absent vs empty": (
        _variant(), _variant(slices__s__description="")),
    "slice mode absent vs exact": (
        _variant(), _variant(slices__s__mode="exact")),
    "component path with and without a trailing slash": (
        _variant(), _variant(components__svc__path="svc/")),
}


class _Configured:
    """A repository whose config can be edited and regenerated."""

    def __init__(self, **extra) -> None:
        scene = Scenario()
        scene.config["components"] = {
            "svc": {
                "path": "svc",
                "boundary": {"provider": "path-hash", "paths": ["api"]},
            },
            "leafy": {"path": "leafy", "boundary": {"provider": "leaf", "paths": []}},
        }
        scene.config.update(extra)
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.file("leafy/index.ts", "export const x = 1;\n")
        scene.commit()
        self.scene = scene

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def generate(self):
        result = run_cli(self.scene.root, "generate", "--source", "head")
        if result.returncode == 0:
            self.scene.commit("lock")
        return result

    def lock(self) -> dict:
        return json.loads(
            (self.scene.root / "boundary.lock.json").read_text(encoding="utf-8")
        )

    def edit(self, mutate) -> None:
        mutate(self.scene.config)
        self.scene.write_config()
        self.scene.commit("edit")

    def verify(self):
        return run_cli(self.scene.root, "verify", "--source", "head")


class AbsentEqualsStatedTests(unittest.TestCase):
    """OBL-HASHING-004 and 015: omitting a default equals stating it."""

    def test_every_equivalent_pair_digests_alike(self):
        for label, (left, right) in EQUIVALENT_PAIRS.items():
            with self.subTest(pair=label):
                self.assertTrue(_same_digest(left, right))

    def test_a_real_change_does_not_digest_alike(self):
        """The premise: the digest is not simply constant."""
        self.assertFalse(
            _same_digest(_variant(), _variant(defaults={"compat_mode": "semver_major_minor"}))
        )
        self.assertFalse(
            _same_digest(_variant(), _variant(components__svc__path="other"))
        )


class DigestInjectivityTests(unittest.TestCase):
    """OBL-HASHING-003: one digest, one lock."""

    def _generated(self, config_edit) -> dict:
        with _Configured() as repo:
            repo.edit(config_edit)
            result = repo.generate()
            self.assertEqual(result.returncode, 0, result.stderr)
            return repo.lock()

    def test_two_spellings_that_share_a_digest_generate_the_same_lock(self):
        for label, edit in (
            ("trailing slash", lambda cfg: cfg["components"]["svc"].__setitem__("path", "svc/")),
            ("stated compat_mode", lambda cfg: cfg.__setitem__("defaults", {"compat_mode": "major"})),
            ("null version_source", lambda cfg: cfg["components"]["svc"].__setitem__("version_source", None)),
        ):
            with self.subTest(spelling=label):
                plain = self._generated(lambda cfg: None)
                other = self._generated(edit)
                self.assertEqual(plain["config_digest"], other["config_digest"])
                self.assertEqual(plain["components"], other["components"])


class PathSpellingTests(unittest.TestCase):
    """OBL-HASHING-005: a fold the schema makes unreachable."""

    def test_a_trailing_slash_shares_a_digest(self):
        self.assertTrue(
            _same_digest(_variant(), _variant(components__svc__path="svc/"))
        )

    def test_the_projection_folds_spellings_validation_rejects(self):
        """The mechanism, stated so a schema relaxation would be visible."""
        for spelling, accepted in FOLDED:
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    _normalized_semantic_path(spelling),
                    _normalized_semantic_path(accepted),
                )
                with self.assertRaises(ValueError):
                    _normalize_declared_path(spelling)

    def test_the_schema_refuses_them_before_the_digest_is_reached(self):
        """Which is why the fold is not reachable from a config today."""
        for spelling, _accepted in FOLDED:
            with self.subTest(spelling=spelling):
                with _Configured() as repo:
                    repo.edit(
                        lambda cfg, value=spelling:
                        cfg["components"]["svc"].__setitem__("path", value)
                    )
                    result = repo.generate()
                    self.assertEqual(result.returncode, COULD_NOT_CHECK)
                    self.assertIn("Schema validation error", result.stderr)

    def test_a_degenerate_selector_is_refused_too(self):
        """The same fold applies to boundary paths, and the same guard."""
        for spelling in ("api//v1.yaml", "api/./v1.yaml", "api/../svc/api"):
            with self.subTest(spelling=spelling):
                with _Configured() as repo:
                    repo.edit(
                        lambda cfg, value=spelling:
                        cfg["components"]["svc"]["boundary"].__setitem__("paths", [value])
                    )
                    result = repo.generate()
                    self.assertEqual(result.returncode, COULD_NOT_CHECK)
                    self.assertIn("Schema validation error", result.stderr)


class UnreachableEquivalenceTests(unittest.TestCase):
    """OBL-HASHING-009 and 015: two comparisons the schema forbids."""

    def test_an_explicit_null_verify_facets_never_reaches_the_digest(self):
        """The obligation compares two configs; only one of them validates."""
        with _Configured(defaults={"verify_facets": None}) as repo:
            repo.edit(lambda cfg: None)
            result = repo.generate()
            self.assertEqual(result.returncode, COULD_NOT_CHECK)
            self.assertIn("verify_facets", result.stderr)
            self.assertIn("not of type 'array'", result.stderr)

    def test_omitting_it_is_the_only_spelling_and_it_verifies(self):
        """The contrast: the reachable half of the pair works."""
        with _Configured() as repo:
            self.assertEqual(repo.generate().returncode, 0)
            self.assertIsNone(repo.lock().get("facet_policy"))
            self.assertEqual(repo.verify().returncode, 0)

    def test_the_projection_does_not_deduplicate_a_consumer_list(self):
        """Pinned as the obligation asks, with the reason it does not matter."""
        self.assertFalse(
            _same_digest(
                _variant(components__svc__consumers=["a"]),
                _variant(components__svc__consumers=["a", "a"]),
            )
        )

    def test_but_a_duplicate_consumer_is_refused_by_the_schema(self):
        with _Configured() as repo:
            repo.edit(
                lambda cfg: cfg["components"]["svc"].__setitem__(
                    "consumers", ["leafy", "leafy"]
                )
            )
            result = repo.generate()
            self.assertEqual(result.returncode, COULD_NOT_CHECK)
            self.assertIn("non-unique elements", result.stderr)


class CompatAliasTests(unittest.TestCase):
    """OBL-HASHING-017: two spellings of one compat mode."""

    def test_the_two_aliases_produce_the_same_fingerprints(self):
        """The premise: nothing about the lock's content moves."""
        with _Configured(defaults={"compat_mode": "major"}) as repo:
            self.assertEqual(repo.generate().returncode, 0)
            before = repo.lock()
            repo.edit(
                lambda cfg: cfg["defaults"].__setitem__("compat_mode", "semver_major")
            )
            self.assertEqual(repo.generate().returncode, 0)
            self.assertEqual(before["components"], repo.lock()["components"])

    def test_changing_between_them_does_not_report_drift(self):
        with _Configured(defaults={"compat_mode": "major"}) as repo:
            self.assertEqual(repo.generate().returncode, 0)
            repo.edit(
                lambda cfg: cfg["defaults"].__setitem__("compat_mode", "semver_major")
            )
            self.assertEqual(repo.verify().returncode, 0, repo.verify().stdout)

    def test_the_alias_change_produces_no_mismatch(self):
        with _Configured(defaults={"compat_mode": "major"}) as repo:
            self.assertEqual(repo.generate().returncode, 0)
            repo.edit(
                lambda cfg: cfg["defaults"].__setitem__("compat_mode", "semver_major")
            )
            verified = repo.verify()
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
            mismatches = [
                line.strip() for line in verified.stdout.splitlines()
                if "MISMATCH" in line
            ]
            self.assertEqual(mismatches, [])

    def test_the_two_aliases_are_identical_in_the_projection(self):
        self.assertTrue(
            _same_digest(
                _variant(defaults={"compat_mode": "major"}),
                _variant(defaults={"compat_mode": "semver_major"}),
            )
        )


class SliceDescriptionTests(unittest.TestCase):
    """OBL-HASHING-016 and 018: a comment edit, and how it is reported."""

    SLICE = {"s": {"components": ["svc"], "description": "first"}}

    def _edited(self):
        repo = _Configured(slices=copy.deepcopy(self.SLICE))
        assert repo.generate().returncode == 0
        before = repo.lock()
        repo.edit(
            lambda cfg: cfg["slices"]["s"].__setitem__("description", "second")
        )
        return repo, before

    def test_the_description_is_stored_in_the_lock(self):
        """Which is why rotating the digest for it is defensible."""
        repo, before = self._edited()
        with repo:
            self.assertEqual(before["slices"]["s"]["description"], "first")

    def test_editing_it_does_not_rotate_the_config_digest(self):
        self.assertTrue(
            _same_digest(
                _variant(slices__s__description="a"),
                _variant(slices__s__description="b"),
            )
        )

    def test_a_component_note_does_not(self):
        """The contrast: the projection drops that annotation and not this one."""
        def with_note(value):
            return _variant(components__svc__boundary={
                "provider": "path-hash", "paths": ["api"], "note": value,
            })

        self.assertTrue(_same_digest(with_note("a"), with_note("b")))

    def test_a_description_edit_reports_no_slice_fingerprint_mismatch(self):
        repo, _before = self._edited()
        with repo:
            slice_lines = [
                line for line in repo.verify().stdout.splitlines()
                if "SLICE MISMATCH" in line
            ]
            self.assertEqual(slice_lines, [])

    def test_regeneration_updates_description_without_rotating_the_fingerprint(self):
        repo, before = self._edited()
        with repo:
            verified = repo.verify()
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
            slice_lines = [
                line.strip() for line in verified.stdout.splitlines()
                if "SLICE MISMATCH" in line
            ]
            self.assertEqual(slice_lines, [])
            self.assertEqual(repo.generate().returncode, 0)
            self.assertEqual(
                before["slices"]["s"]["fingerprint"],
                repo.lock()["slices"]["s"]["fingerprint"],
            )
            self.assertEqual(repo.lock()["slices"]["s"]["description"], "second")


if __name__ == "__main__":
    unittest.main()
