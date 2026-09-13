"""Several ways to select no facets, and what each of them turns out to mean.

`--facets` takes a comma-separated list. A value that contains no facet name
at all - a space, a comma, two commas - is not a selection, and every such
value should mean the same thing. What decides the meaning today is whether
the string survives `.strip()`: a space does not and yields the implicit
per-component policy, a comma does and is read as an explicit CLI-wide
selection of all four facets, which is a stricter gate than the default.

Covers OBL-PROVIDERS-003.
"""

from __future__ import annotations

import unittest

from boundver.core import FACETS, _parse_facets_arg

from tests._parity import run_cli
from tests._scenarios import Scenario

COULD_NOT_CHECK = 2

#: Values a user could type that select nothing at all.
EMPTY_SPELLINGS = ("", " ", ",", ",,", " , ", "\t")


class _Locked:
    """A clean repository whose lock verifies."""

    def __init__(self) -> None:
        scene = Scenario()
        scene.component("svc", path="svc", boundary=["api"])
        scene.component("leafy", path="leafy", provider="leaf")
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.file("leafy/index.ts", "export const x = 1;\n")
        scene.commit()
        assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
        scene.commit("lock")
        self.scene = scene

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def verify(self, facets=None):
        arguments = ["verify", "--source", "head"]
        if facets is not None:
            arguments += ["--facets", facets]
        return run_cli(self.scene.root, *arguments)


class ConfiguredDefaultTests(unittest.TestCase):
    """An unset --facets must fall back to the configured default.

    Every existing call passes an empty config, so `configured` is always
    empty and the fallback lands on FACETS whichever branch runs. Deleting
    the fallback to the configured value entirely left all of them green
    (MUT-PROVIDERS-206), which would silently widen verification to every
    facet for any project that narrowed it on purpose.
    """

    @staticmethod
    def _config(facets):
        return {"defaults": {"verify_facets": facets}}

    def test_an_empty_argument_uses_the_configured_facets(self):
        for configured in (["exact"], ["boundary", "compat"], ["behavior"]):
            with self.subTest(configured=configured):
                self.assertEqual(
                    _parse_facets_arg("", self._config(configured)),
                    sorted(configured),
                )

    def test_an_explicit_argument_overrides_the_configured_facets(self):
        """The contrast: the configured value is a default, not a floor."""
        self.assertEqual(
            _parse_facets_arg("exact", self._config(["boundary", "compat"])),
            ["exact"],
        )

    def test_an_empty_configured_list_still_falls_back_to_every_facet(self):
        """The premise the existing empty-config cases rest on."""
        self.assertEqual(_parse_facets_arg("", self._config([])), sorted(FACETS))
        self.assertEqual(_parse_facets_arg("", {}), sorted(FACETS))


class EmptyFacetSelectionTests(unittest.TestCase):
    """OBL-PROVIDERS-003: one meaning for a value that names nothing."""

    def test_the_repository_verifies_with_no_flag_at_all(self):
        """The premise: nothing here is drifting."""
        with _Locked() as repo:
            self.assertEqual(repo.verify().returncode, 0, repo.verify().stdout)

    def test_no_spelling_contains_a_facet_name(self):
        """The premise: these really are empty selections."""
        for spelling in EMPTY_SPELLINGS:
            with self.subTest(spelling=spelling):
                named = [part.strip() for part in spelling.split(",") if part.strip()]
                self.assertEqual(named, [])

    def test_every_empty_spelling_gives_the_same_exit_code(self):
        """Every spelling that names no facet uses the configured policy."""
        with _Locked() as repo:
            codes = {
                spelling: repo.verify(spelling).returncode
                for spelling in EMPTY_SPELLINGS
            }
            self.assertEqual(set(codes.values()), {0}, codes)

    def test_empty_spellings_all_use_the_implicit_policy(self):
        """Separators alone must not turn an implicit policy into an explicit one."""
        with _Locked() as repo:
            codes = {
                spelling: repo.verify(spelling).returncode
                for spelling in EMPTY_SPELLINGS
            }
        for spelling, code in codes.items():
            with self.subTest(spelling=spelling):
                self.assertEqual(code, 0)

    def test_the_parsed_facet_list_is_the_same_either_way(self):
        """So the difference is the explicitness, not the selection."""
        for spelling in EMPTY_SPELLINGS:
            with self.subTest(spelling=spelling):
                self.assertEqual(_parse_facets_arg(spelling, {}), sorted(FACETS))

    def test_an_empty_selection_does_not_report_unavailable_facets(self):
        """Separators alone do not request every undeclared facet explicitly."""
        with _Locked() as repo:
            result = repo.verify(",")
            self.assertEqual(result.returncode, 0)
            unavailable = [
                line.strip() for line in result.stdout.splitlines()
                if "UNAVAILABLE FACET" in line
            ]
            self.assertEqual(unavailable, [], result.stdout)

    def test_naming_an_available_facet_explicitly_still_passes(self):
        """The contrast: the strict reading is fine when the facet is there.

        `exact` is computed for every component, so gating on it explicitly
        passes. `boundary` is not - the leaf component has none - so naming it
        refuses, which is the strict policy working as intended. That is what
        makes the comma a defect rather than a preference: it opts a user into
        that policy without them naming a facet.
        """
        with _Locked() as repo:
            self.assertEqual(repo.verify("exact").returncode, 0)
            self.assertEqual(repo.verify("boundary").returncode, COULD_NOT_CHECK)


if __name__ == "__main__":
    unittest.main()
