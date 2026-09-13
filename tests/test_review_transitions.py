"""What a range review tells a reader when nothing in a component moved.

Two kinds of change leave every facet digest alone. One is at the lock level -
the project name, the config contract, the config digest - and one is at the
component level, where consumers or a version moved but no artifact did. Both
matter to a reviewer, and both are invisible in a report that only counts
changed components.

The JSON and text views both record them.

Covers OBL-HASHING-001 and OBL-HASHING-010.
"""

from __future__ import annotations

import json
import unittest

from boundver._lockfile import COMPONENT_METADATA_FIELDS
from boundver._review import analyze_review_range

from tests._parity import run_cli
from tests._scenarios import Scenario


def _declare_default(scene) -> None:
    """One component with a boundary and one leaf consumer of it."""
    scene.component("svc", path="svc", boundary=["api"], consumers=["sdk"])
    scene.component("sdk", path="sdk", provider="leaf")
    scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
    scene.file("sdk/index.ts", "export const x = 1;\n")


def _declare_versioned(scene) -> None:
    """The same two components, plus two manifests naming different versions.

    Pointing ``version_source`` at the other manifest moves the component's
    recorded version without touching a tracked byte inside the component, so
    the change stays metadata-only. The two versions share a major, which
    keeps the compat facet still as well.
    """
    _declare_default(scene)
    scene.component(
        "svc", path="svc", boundary=["api"], consumers=["sdk"],
        version_source={"file": "a.json", "field": "version"},
    )
    scene.json_file("svc/a.json", {"version": "1.0.0"})
    scene.json_file("svc/b.json", {"version": "1.2.0"})


class _Range:
    """A base and a target, with one edit applied between them."""

    def __init__(self, edit, declare=_declare_default) -> None:
        scene = Scenario()
        declare(scene)
        scene.commit()
        assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
        scene.commit("lock")
        self.base = scene.head()
        edit(scene)
        scene.commit("edit")
        assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
        scene.commit("relock")
        self.scene = scene
        self.target = scene.head()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def text(self) -> str:
        result = run_cli(
            self.scene.root, "review", "--base", self.base, "--target", self.target,
            "--format", "text",
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    def document(self) -> dict:
        result = run_cli(
            self.scene.root, "review", "--base", self.base, "--target", self.target,
            "--format", "json",
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def built(self) -> dict:
        """The result object itself, as the text renderer receives it.

        `document` reads the CLI's JSON view, and that view is serialised with
        sorted keys, so it cannot show the order in which the analysis stored a
        mapping. A premise about insertion order has to look here instead.
        """
        return analyze_review_range(self.scene.root, self.base, self.target)


def _rename_project(scene) -> None:
    scene.config["project"] = "renamed"
    scene.write_config()


def _edit_config(**fields):
    def edit(scene):
        scene.config["components"]["svc"].update(fields)
        scene.write_config()

    return edit


#: Read the version from the other manifest, which moves ``version`` and the
#: ``semver`` reduction derived from it and leaves every facet alone.
_switch_version_manifest = _edit_config(
    version_source={"file": "b.json", "field": "version"}
)


def _labelled(text: str, prefix: str) -> list:
    return [line.strip() for line in text.splitlines() if line.strip().startswith(prefix)]


class LockMetadataTransitionTests(unittest.TestCase):
    """OBL-HASHING-001: a lock-level rename is a change a reviewer must see."""

    def test_the_json_view_records_the_transition(self):
        """The premise: the value is computed and available to render."""
        with _Range(_rename_project) as review:
            metadata = review.document()["metadata"]
            self.assertEqual(
                metadata["project"], {"before": "scenario", "after": "renamed"}
            )
            self.assertIn("config_digest", metadata)

    def test_the_text_view_names_the_old_and_new_project(self):
        with _Range(_rename_project) as review:
            self.assertIn(
                'project: "scenario" -> "renamed"',
                review.text(),
            )

    def test_metadata_only_change_does_not_read_as_an_empty_report(self):
        """Zero component counts coexist with an explicit metadata section."""
        with _Range(_rename_project) as review:
            text = review.text()
            for section in ("CHANGED COMPONENTS (0)", "CHANGED SLICES (0)",
                            "IMPACTED SLICES (0)"):
                self.assertIn(section, text)
            self.assertIn("LOCKFILE METADATA CHANGES (2)", text)
            self.assertIn('project: "scenario" -> "renamed"', text)
            self.assertIn("config_digest:", text)

    def test_a_component_change_is_reported_in_both_views(self):
        """The contrast: the text view is not silent about everything."""
        def edit(scene):
            scene.append_line("svc/api/v1.yaml", "change\n")

        with _Range(edit) as review:
            self.assertIn("CHANGED COMPONENTS (1)", review.text())
            self.assertEqual(review.document()["summary"]["changed_components"], 1)


class MetadataOnlyComponentTests(unittest.TestCase):
    """OBL-HASHING-010: no facet moved, and the report says which field did."""

    def test_a_single_metadata_field_renders_both_lines(self):
        cases = {
            "external_consumers": _edit_config(external_consumers=["mobile"]),
            "consumers": _edit_config(consumers=[]),
        }
        for field, edit in cases.items():
            with self.subTest(field=field):
                with _Range(edit) as review:
                    text = review.text()
                    self.assertEqual(
                        _labelled(text, "Facets:"), ["Facets: none (metadata-only change)"]
                    )
                    self.assertEqual(_labelled(text, "Metadata:"), [f"Metadata: {field}"])

    def test_two_fields_are_comma_joined_and_sorted(self):
        edit = _edit_config(consumers=[], external_consumers=["mobile"])
        with _Range(edit) as review:
            self.assertEqual(
                _labelled(review.text(), "Metadata:"),
                ["Metadata: consumers, external_consumers"],
            )

    def test_the_listed_fields_are_the_ones_the_document_carries(self):
        """Exactly those, and no others."""
        edit = _edit_config(consumers=[], external_consumers=["mobile"])
        with _Range(edit) as review:
            listed = _labelled(review.text(), "Metadata:")[0]
            fields = listed.split(": ", 1)[1].split(", ")
            self.assertEqual(fields, sorted(fields))
            self.assertEqual(set(fields), {"consumers", "external_consumers"})

    def test_the_field_list_is_alphabetical_rather_than_table_order(self):
        """MUT-HASHING-482: the sort in the metadata line has to do real work.

        Every other case in this class moves the pair ``consumers`` and
        ``external_consumers``. Those two are neighbours in
        ``COMPONENT_METADATA_FIELDS`` in the order the alphabet already puts
        them, so sorting them changes nothing and a renderer that joined the
        mapping as it stood printed exactly the same line. Dropping the sort
        was therefore invisible to this file.

        This case moves ``version`` instead, which drags the ``semver``
        reduction along with it, and the table lists those two in the reverse
        of alphabetical order. Only a renderer that sorts can produce the text
        asserted here.
        """
        with _Range(_switch_version_manifest, declare=_declare_versioned) as review:
            text = review.text()
            self.assertEqual(
                _labelled(text, "Facets:"), ["Facets: none (metadata-only change)"]
            )
            self.assertEqual(
                _labelled(text, "Metadata:"), ["Metadata: semver, version"]
            )

    def test_the_analysis_hands_the_renderer_those_fields_unsorted(self):
        """The premise for MUT-HASHING-482: the sort has something to undo.

        The renderer reads ``component["metadata"]``, and the analysis builds
        that mapping by walking ``COMPONENT_METADATA_FIELDS``, so its keys
        arrive in the order of that tuple. If the tuple happened to list
        ``version`` after ``semver``, or if this range moved only one field,
        the assertion above would hold for a renderer that never sorted
        anything. So assert both halves: the table really is the reverse of
        alphabetical at these two positions, and this range really does deliver
        both keys in that reversed order.

        The order is read off the result object rather than the CLI's JSON
        view, because that view is serialised with sorted keys and would show
        the alphabet whatever the analysis stored.
        """
        table = list(COMPONENT_METADATA_FIELDS)
        self.assertLess(table.index("version"), table.index("semver"))
        self.assertLess("semver", "version")
        with _Range(_switch_version_manifest, declare=_declare_versioned) as review:
            carried = {
                component["name"]: list(component["metadata"])
                for component in review.built()["components"]["changed"]
            }
            self.assertEqual(carried, {"svc": ["version", "semver"]})
            self.assertNotEqual(carried["svc"], sorted(carried["svc"]))

    def test_the_same_fixture_names_whichever_field_actually_moved(self):
        """The contrast for MUT-HASHING-482: the line is not a fixed string.

        A test that only ever demanded ``Metadata: semver, version`` would
        still pass against a renderer that printed that literal for every
        component. The same repository, edited so that only ``consumers``
        moves, has to name ``consumers`` and nothing else.
        """
        with _Range(_edit_config(consumers=[]), declare=_declare_versioned) as review:
            self.assertEqual(
                _labelled(review.text(), "Metadata:"), ["Metadata: consumers"]
            )

    def test_a_facet_change_renders_the_facets_instead(self):
        """The contrast: a real move prints one line per facet and no summary."""
        def edit(scene):
            scene.append_line("svc/api/v1.yaml", "change\n")

        with _Range(edit) as review:
            text = review.text()
            self.assertEqual(_labelled(text, "Metadata:"), [])
            self.assertEqual(_labelled(text, "Facets:"), [])
            moved = [
                line.strip() for line in text.splitlines()
                if line.strip().startswith(("exact:", "boundary:"))
            ]
            self.assertEqual(len(moved), 2, moved)
            for line in moved:
                self.assertIn(" -> ", line)


if __name__ == "__main__":
    unittest.main()
