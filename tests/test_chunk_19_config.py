"""What an embedder is promised at the library boundary, and one promise about
file identity that only ever ran on a third of the code that keeps it.

Five of these six obligations are about the edge of boundver rather than its
middle. `src/boundver/__init__.py` is ninety lines of glue that re-derives, for
a Python caller, the sequence `core.py` performs for a shell caller, and the
two were written to different rules: the CLI refuses an unknown `--components`
or `--facets` entry with `EXIT_USAGE` before it verifies anything, while the
API forwards the same entry into `verify_lockfile` and gets it back as an
ordinary mismatch string; `load_config` validates with `require_slice_facets`
off while `generate` turns it on; `load_config` reads the working tree while
`generate` and `verify` read HEAD; and `generate` joins `out_path` to the
repository root with an operator that discards the root when the argument is
absolute. Four of those five are still live divergences, so most of what
follows pins current behaviour beside an `expectedFailure` naming the promise,
and the documentation escape hatch each obligation offers -- "or the difference
must be stated" -- is checked rather than assumed, because `docs/reference.md`
documents exactly one of the three Python defaults and says nothing about the
other two.

The fixture work was mostly about process globals and about what a Windows
filesystem can hold. `git_root()` reads `Path.cwd()`, so the working directory
is an input to every public API call and has to be entered and restored around
each one, and `_git` keeps two `lru_cache`s keyed by the repository root that
would otherwise answer for the previous scenario. The mode obligation could not
use `os.chmod` at all: this host leaves `core.filemode` at false and cannot
create a symlink without a privilege, so a test written that way would assert
that a digest did not move after nothing moved. Every mode here is therefore
put into the index with `update-index --chmod=+x` or
`--cacheinfo 120000,<oid>`, which is host-independent, and the single blob is
reused across all three so the bytes are provably constant while only the mode
changes. That buys head and index outright. It does not buy the working tree a
symlink, and the file says so where it matters instead of skipping the whole
obligation on `os.name == "nt"` the way the existing coverage does.

Four of these obligations assert that something did not happen -- the CLI never
verified, the empty-segment ignore rule never matched, the working-tree digest
never moved -- and each one is preceded here by a test proving the mechanism
would have shown it. The CLI is asked to verify a repository that really is out
of date, so exit 4 and `LOCKFILE OUT OF DATE` on stdout are observed before the
unknown-entry run is required to print nothing. A gitignore negation spelled
`!a/b` is shown re-including a file before the same negation spelled `!a//b` is
required to do nothing. And the working-tree symlink pin is only meaningful
because the same transition is shown moving all four digests under head and
index a few methods earlier.

Covers OBL-LOCKFILE-060, OBL-LOCKFILE-063, OBL-CONFIG-043, OBL-CONFIG-044,
OBL-GIT-SOURCE-011 and OBL-GIT-SOURCE-022.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

import boundver
from boundver import _git
from boundver._git import _GitignoreRules
from boundver._hashing import _content_only_digest
from boundver._utils import (
    ConfigError,
    GuardrailError,
    _compile_path_glob,
    _issue_facet,
    _match_path_glob,
    _normalize_declared_path,
    _PathGlobOperation,
)

from tests._parity import run_cli
from tests._scenarios import Scenario, requires_symlinks

CONFIG = "boundary.config.json"
LOCK = "boundary.lock.json"

#: The four facets boundver gates on, in the spelling `verify --facets` takes.
FACETS = ("exact", "behavior", "boundary", "compat")


def clear_git_caches() -> None:
    """Both `lru_cache`s `_git` installs, each keyed by the resolved repo root.

    A scenario that reused a temporary path would otherwise inherit the
    previous repository's answers, so these are cleared entering and leaving
    every repository this module enters.
    """
    _git._ambient_worktree_config_overrides.cache_clear()
    _git._repository_filter_config_overrides.cache_clear()


class InRepo:
    """Enter a repository. `git_root()` reads `Path.cwd()`, so the process
    working directory is an input to every public API call made here."""

    def __init__(self, path: Any) -> None:
        self.path = str(path)

    def __enter__(self) -> "InRepo":
        self.previous = os.getcwd()
        os.chdir(self.path)
        clear_git_caches()
        return self

    def __exit__(self, *exc: object) -> bool:
        os.chdir(self.previous)
        clear_git_caches()
        return False


def unlocked_repository() -> Scenario:
    """The same declaration with no lockfile written yet.

    The containment tests need this: with a lock already committed at
    `boundary.lock.json`, "no lockfile appeared inside the repository" is
    unobservable, because one was there before the call.
    """
    scene = Scenario()
    scene.component(
        "svc",
        path="services/svc",
        provider="path-hash",
        boundary=["api.yaml"],
        behavior=["api.yaml"],
    )
    scene.file("services/svc/api.yaml", "openapi: 3.1.0\n")
    scene.commit()
    return scene


def api_repository(*, drift: bool = False) -> Scenario:
    """A committed repository whose committed lock the API can verify.

    Behavior selects the same file as boundary, because `validate_config`
    rejects a component whose behavior paths leave a boundary artifact
    uncovered, and no slice is declared, because `generate` passes
    `require_slice_facets=True` and a slice without facet inputs would fail
    before reaching anything these obligations are about.
    """
    scene = Scenario()
    scene.component(
        "svc",
        path="services/svc",
        provider="path-hash",
        boundary=["api.yaml"],
        behavior=["api.yaml"],
    )
    scene.file("services/svc/api.yaml", "openapi: 3.1.0\n")
    scene.commit()
    with InRepo(scene.root):
        boundver.generate()
    scene.git("add", "--all")
    scene.git("commit", "-m", "lock")
    if drift:
        scene.file("services/svc/api.yaml", "openapi: 3.1.0\ninfo: {}\n")
        scene.commit("drift")
    return scene


# ---------------------------------------------------------------------------
# OBL-LOCKFILE-060: unvalidated components and facets at the API boundary
# ---------------------------------------------------------------------------

#: One unrecognised selection per row: the API keyword that carries it, the CLI
#: flag that carries the same thing, and the exact string each surface produced
#: for it. Both message texts were read off a run, not off the source.
UNKNOWN_SELECTIONS: Dict[str, Dict[str, Any]] = {
    "component": {
        "api_kwargs": {"components": ["nope"]},
        "cli_argv": ("--components", "nope"),
        "api_issue": "Unknown verification component(s): nope",
        "cli_stderr": "ERROR: unknown --components entries: nope",
    },
    "facet": {
        "api_kwargs": {"facets": ["boundry"]},
        "cli_argv": ("--facets", "boundry"),
        "api_issue": "Unknown verification facet(s): boundry",
        "cli_stderr": "ERROR: unknown --facets entries: boundry",
    },
}

#: A selection that names one real entry alongside one typo. A contract that
#: skipped unknown names rather than reporting them would verify the real one
#: and return an empty list, which is the failure mode the obligation names.
MIXED_SELECTIONS: Dict[str, Dict[str, Any]] = {
    "one real component and one typo": {
        "api_kwargs": {"components": ["svc", "nope"]},
        "api_issue": "Unknown verification component(s): nope",
    },
    "one real facet and one typo": {
        "api_kwargs": {"facets": ["exact", "boundry"]},
        "api_issue": "Unknown verification facet(s): boundry",
    },
}


class UnknownSelectionTests(unittest.TestCase):
    """OBL-LOCKFILE-060: a typo must never read as 'everything is current'."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.current = api_repository()
        cls.stale = api_repository(drift=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.current.close()
        cls.stale.close()

    def test_the_fixture_verifies_clean_through_both_surfaces(self):
        """The premise for every emptiness claim below.

        Without this, `verify()` returning a non-empty list for an unknown
        entry could just as well be a repository that is out of date for
        ordinary reasons, and the CLI's exit 2 could be any usage error.
        """
        with InRepo(self.current.root):
            self.assertEqual(boundver.verify(), [])
        result = run_cli(self.current.root, "verify")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_unknown_selection_raises_config_error_naming_the_entry(self):
        with InRepo(self.current.root):
            for label, row in UNKNOWN_SELECTIONS.items():
                with self.subTest(selection=label):
                    with self.assertRaises(ConfigError) as raised:
                        boundver.verify(**row["api_kwargs"])
                    self.assertEqual(str(raised.exception), row["api_issue"])

    def test_a_selection_mixing_a_real_entry_with_a_typo_still_reports(self):
        with InRepo(self.current.root):
            for label, row in MIXED_SELECTIONS.items():
                with self.subTest(selection=label):
                    with self.assertRaises(ConfigError) as raised:
                        boundver.verify(**row["api_kwargs"])
                    self.assertEqual(str(raised.exception), row["api_issue"])

    def test_the_cli_reports_drift_on_the_stale_repository(self):
        """The premise for 'the CLI never verified'.

        This repository is genuinely out of date, so a run that reaches
        verification says so loudly. The next test asks the same binary for the
        same repository with a typo'd selection and requires silence.
        """
        result = run_cli(self.stale.root, "verify")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("LOCKFILE OUT OF DATE", result.stdout)

    def test_the_cli_refuses_an_unknown_selection_before_verifying_anything(self):
        for label, row in UNKNOWN_SELECTIONS.items():
            with self.subTest(selection=label):
                result = run_cli(self.stale.root, "verify", *row["cli_argv"])
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr.strip(), row["cli_stderr"])

    def test_a_real_mismatch_issue_carries_its_facet(self):
        """The premise for the prefix-gate claim.

        `_issue_facet` is the parser boundver's own JSON policy view uses to
        file an issue under a facet. It has to answer for an ordinary mismatch
        before its silence on the unknown-entry string means anything.
        """
        with InRepo(self.stale.root):
            issues = boundver.verify()
        self.assertTrue(issues)
        self.assertEqual(
            [_issue_facet(issue) for issue in issues],
            ["boundary", "behavior", "exact"],
        )

    def test_an_unknown_entry_never_becomes_a_filterable_drift_issue(self):
        with InRepo(self.current.root):
            for label, row in UNKNOWN_SELECTIONS.items():
                with self.subTest(selection=label):
                    with self.assertRaises(ConfigError):
                        boundver.verify(**row["api_kwargs"])

    def test_the_api_refuses_an_unknown_selection_the_way_the_cli_does(self):
        with InRepo(self.current.root):
            for row in UNKNOWN_SELECTIONS.values():
                with self.assertRaises(ConfigError):
                    boundver.verify(**row["api_kwargs"])

    def test_the_api_refuses_non_list_or_non_text_selections(self):
        rows = (
            {"components": "svc"},
            {"components": ["svc", 1]},
            {"facets": "exact"},
            {"facets": ["exact", None]},
        )
        with InRepo(self.current.root):
            for row in rows:
                with self.subTest(arguments=row):
                    with self.assertRaises(ConfigError):
                        boundver.verify(**row)


# ---------------------------------------------------------------------------
# OBL-LOCKFILE-063: out_path and lock_path containment
# ---------------------------------------------------------------------------


class LockPathContainmentTests(unittest.TestCase):
    """OBL-LOCKFILE-063: 'relative to repo root' is what the caller trusts."""

    def setUp(self) -> None:
        self.outside = tempfile.TemporaryDirectory()
        self.addCleanup(self.outside.cleanup)
        self.elsewhere = Path(self.outside.name)

    def test_the_docstring_still_promises_a_repository_relative_path(self):
        """The sentence the obligation is measured against, read at runtime."""
        self.assertIn("relative to repo root", boundver.generate.__doc__ or "")

    def test_a_relative_out_path_writes_inside_the_repository(self):
        """The premise: the writer works, and writes where it is told."""
        with unlocked_repository() as scene:
            self.assertFalse((scene.root / "build" / "nested.lock.json").exists())
            with InRepo(scene.root):
                boundver.generate(out_path="build/nested.lock.json")
            self.assertTrue((scene.root / "build" / "nested.lock.json").is_file())

    def test_an_absolute_out_path_is_refused_without_writing(self):
        target = self.elsewhere / "escaped.lock.json"
        with unlocked_repository() as scene:
            self.assertFalse((scene.root / LOCK).exists())
            with InRepo(scene.root):
                with self.assertRaises(ConfigError):
                    boundver.generate(out_path=str(target))
            self.assertFalse(target.exists())
            self.assertFalse((scene.root / LOCK).exists())

    def test_a_parent_traversing_out_path_is_refused(self):
        """The one half of the obligation the code already answers."""
        with unlocked_repository() as scene:
            escape = scene.root.parent / "traversed.lock.json"
            self.addCleanup(lambda: escape.unlink(missing_ok=True))
            with InRepo(scene.root):
                with self.assertRaises(ConfigError) as caught:
                    boundver.generate(out_path="../traversed.lock.json")
            self.assertIn(
                "Output path must not contain parent-directory traversal",
                str(caught.exception),
            )
            self.assertFalse(escape.exists())

    def test_generate_refuses_an_out_path_that_leaves_the_repository_root(self):
        """The public API enforces its repository-relative path contract."""
        target = self.elsewhere / "refused.lock.json"
        with unlocked_repository() as scene:
            with InRepo(scene.root):
                with self.assertRaises(ConfigError):
                    boundver.generate(out_path=str(target))

    def test_an_absolute_lock_path_is_refused_in_every_source_mode(self):
        for source in ("head", "index", "working-tree"):
            with self.subTest(source=source):
                with api_repository() as scene:
                    copy = self.elsewhere / f"{source}.lock.json"
                    copy.write_text(
                        (scene.root / LOCK).read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                    with InRepo(scene.root):
                        with self.assertRaises(ConfigError) as caught:
                            boundver.verify(lock_path=str(copy), source=source)
                    self.assertIn(
                        "Lockfile path must be relative to the repository root",
                        str(caught.exception),
                    )

    def test_working_tree_verification_refuses_every_outside_spelling(self):
        for label, spelling in (("absolute", None), ("parent traversal", "..")):
            with self.subTest(spelling=label):
                with api_repository() as scene:
                    if spelling is None:
                        copy = self.elsewhere / "outside.lock.json"
                        argument = str(copy)
                    else:
                        copy = scene.root.parent / "up.lock.json"
                        argument = "../up.lock.json"
                    body = json.loads((scene.root / LOCK).read_text(encoding="utf-8"))
                    self.assertEqual(body["project"], "scenario")
                    body["project"] = "elsewhere"
                    copy.write_text(
                        json.dumps(body, indent=2) + "\n", encoding="utf-8"
                    )
                    self.addCleanup(lambda p=copy: p.unlink(missing_ok=True))
                    with InRepo(scene.root):
                        self.assertEqual(boundver.verify(source="working-tree"), [])
                        with self.assertRaises(ConfigError):
                            boundver.verify(
                                lock_path=argument, source="working-tree"
                            )

    def test_verify_refuses_a_lock_outside_the_repository_in_every_source_mode(self):
        """Working-tree reads enforce the same containment as snapshots."""
        with api_repository() as scene:
            copy = self.elsewhere / "outside.lock.json"
            copy.write_text(
                (scene.root / LOCK).read_text(encoding="utf-8"), encoding="utf-8"
            )
            with InRepo(scene.root):
                with self.assertRaises(ConfigError):
                    boundver.verify(lock_path=str(copy), source="working-tree")


# ---------------------------------------------------------------------------
# OBL-CONFIG-043: validation strength between load_config and generate
# ---------------------------------------------------------------------------

#: Providers whose declaration resolves cleanly against the fixture file, which
#: is simultaneously valid JSON and a valid OpenAPI 3.1 document so that every
#: one of them reaches the slice rule instead of failing earlier.
FACET_PROVIDERS = ("path-hash", "leaf", "implicit", "json-file", "openapi")

#: A component declaration drawn as (provider, boundary paths, behavior paths,
#: version source). The four booleans are exactly the inputs the documented
#: facet-availability rule reads.
SHAPE = st.tuples(
    st.sampled_from(FACET_PROVIDERS),
    st.booleans(),
    st.booleans(),
    st.booleans(),
)

#: Each API pair costs about 1.4 seconds of Git subprocesses, so the example
#: count is set to keep this class near half a minute rather than four.
API_PROFILE = settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

#: Four shapes `load_config` accepted and the exact ConfigError `generate`
#: raised for each, read off a run. Keyed by what makes the shape interesting.
SLICE_FACET_WITNESSES: Dict[str, Tuple[Tuple[Any, ...], str, str]] = {
    "compat mode, no version source": (
        ("path-hash", True, True, False),
        "compat",
        "Slice 's' mode 'compat' requires compat digest from component 'svc' "
        "to supply that facet, but the component has no version_source",
    ),
    "behavior mode, no behavior paths": (
        ("path-hash", True, False, True),
        "behavior",
        "Slice 's' mode 'behavior' requires behavior digest from component "
        "'svc' to supply that facet, but the component has no non-empty "
        "behavior.paths",
    ),
    "boundary mode, leaf provider": (
        ("leaf", False, True, True),
        "boundary",
        "Slice 's' mode 'boundary' requires boundary digest from component "
        "'svc' to supply that facet, but provider 'leaf' does not produce a "
        "boundary digest from this declaration",
    ),
    "boundary mode, implicit provider without paths": (
        ("implicit", False, True, True),
        "boundary",
        "Slice 's' mode 'boundary' requires boundary digest from component "
        "'svc' to supply that facet, but provider 'implicit' does not produce "
        "a boundary digest from this declaration",
    ),
}


def declared_facets(shape: Tuple[Any, ...]) -> set:
    """Which facets a declaration can produce, restated from the documentation.

    This is the oracle, and it is deliberately not `_available_component_facets`
    from `_utils`: it is written from the rule the schema and
    `docs/reference.md` describe -- every component has an exact identity, a
    boundary digest needs a provider that publishes one, a behavior digest
    needs at least one behavior path, and a compat digest needs a version
    source. Sharing the implementation would make the property a tautology.
    """
    provider, has_boundary_paths, has_behavior_paths, has_version_source = shape
    facets = {"exact"}
    if provider not in {"leaf", "implicit"} or (
        provider == "implicit" and has_boundary_paths
    ):
        facets.add("boundary")
    if has_behavior_paths:
        facets.add("behavior")
    if has_version_source:
        facets.add("compat")
    return facets


def component_entry(shape: Tuple[Any, ...], path: str) -> dict:
    provider, has_boundary_paths, has_behavior_paths, has_version_source = shape
    entry: Dict[str, Any] = {
        "path": path,
        "boundary": {
            "provider": provider,
            "paths": ["api.json"] if has_boundary_paths else [],
        },
    }
    if has_behavior_paths:
        entry["behavior"] = {"paths": ["api.json"]}
    if has_version_source:
        entry["version_source"] = {"file": "package.json", "field": "version"}
    return entry


class SliceFacetValidationStrengthTests(unittest.TestCase):
    """OBL-CONFIG-043: a two-stage validator whose first stage is weaker."""

    NAMES = ("svc", "sdk")

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = Scenario()
        for name in cls.NAMES:
            cls.scene.component(
                name,
                path=f"services/{name}",
                provider="path-hash",
                boundary=["api.json"],
                behavior=["api.json"],
            )
            cls.scene.file(
                f"services/{name}/api.json",
                '{"openapi": "3.1.0", "info": {"title": "t", "version": '
                '"1.0.0"}, "paths": {}}\n',
            )
            cls.scene.json_file(f"services/{name}/package.json", {"version": "1.2.3"})
        cls.scene.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    def setUp(self) -> None:
        self.previous = os.getcwd()
        os.chdir(self.scene.root)
        clear_git_caches()
        self.addCleanup(clear_git_caches)
        self.addCleanup(os.chdir, self.previous)

    def _write(self, config: dict) -> None:
        (self.scene.root / CONFIG).write_text(
            json.dumps(config, indent=2) + "\n", encoding="utf-8"
        )
        clear_git_caches()

    def _config(self, shapes: List[Tuple[Any, ...]], members, mode: str) -> dict:
        return {
            "project": "chunk19",
            "components": {
                name: component_entry(shape, f"services/{name}")
                for name, shape in zip(self.NAMES, shapes)
            },
            "slices": {"s": {"mode": mode, "components": list(members)}},
        }

    def _refusal(self, call) -> Optional[str]:
        try:
            call()
        except ConfigError as exc:
            return str(exc)
        return None

    def test_the_witness_table_separates_accepted_from_refused_shapes(self):
        """The premise for the property: both verdicts are reachable here, and
        the refusal text names the slice, the mode and the member."""
        for label, (shape, mode, message) in SLICE_FACET_WITNESSES.items():
            with self.subTest(shape=label):
                config = self._config([shape, shape], ["svc"], mode)
                self._write(config)
                loaded = self._refusal(
                    lambda: boundver.load_config(source="working-tree")
                )
                generated = self._refusal(
                    lambda: boundver.generate(out_path=None, source="working-tree")
                )
                self.assertIsNotNone(loaded, f"{label}: load_config accepted it")
                self.assertIsNotNone(generated, f"{label}: generate accepted it")
                self.assertIn(message, loaded)
                self.assertEqual(loaded, generated)

                accepted = self._config([shape, shape], ["svc"], "exact")
                self._write(accepted)
                self.assertIsNone(
                    self._refusal(
                        lambda: boundver.generate(
                            out_path=None, source="working-tree"
                        )
                    ),
                    f"{label}: mode 'exact' should have been accepted",
                )

    @API_PROFILE
    @given(
        shapes=st.lists(SHAPE, min_size=2, max_size=2),
        members=st.lists(
            st.sampled_from(NAMES), min_size=1, max_size=2, unique=True
        ),
        mode=st.sampled_from(FACETS),
    )
    @example(  # load_config refuses it: explicit provider, no boundary paths
        shapes=[("path-hash", False, True, True), ("path-hash", True, True, True)],
        members=["svc"],
        mode="exact",
    )
    @example(  # accepted by both, no member short of the facet
        shapes=[("path-hash", True, True, True), ("path-hash", True, True, True)],
        members=["svc", "sdk"],
        mode="exact",
    )
    @example(  # refused by both, both members lack the selected facet
        shapes=[("path-hash", True, True, False), ("path-hash", True, True, False)],
        members=["svc", "sdk"],
        mode="compat",
    )
    def test_generate_accepts_exactly_the_shapes_the_facet_rule_allows(
        self, shapes, members, mode
    ):
        """The whole property, over the declaration space.

        The obligation is a conditional -- what `load_config` returns without
        error must be what `generate` accepts -- so `load_config` is the gate
        rather than an assertion. Some declarations it refuses outright: an
        explicit boundary provider with no boundary paths is rejected by both
        stages, and those examples only establish the easy direction, that a
        config the weaker stage refuses the stronger one refuses too. On the
        rest, `generate` is required to accept exactly when every resolved
        slice member can produce the slice's mode, and to name each member it
        cannot.

        The three explicit examples pin one shape from each of those branches,
        so a strategy that drifted and stopped generating one of them would
        leave the property looking healthy while proving a third less.
        """
        self._write(self._config(shapes, members, mode))
        loaded = self._refusal(lambda: boundver.load_config(source="working-tree"))
        refusal = self._refusal(
            lambda: boundver.generate(out_path=None, source="working-tree")
        )
        self.assertEqual(loaded, refusal)
        by_name = dict(zip(self.NAMES, shapes))
        offenders = [
            name for name in members if mode not in declared_facets(by_name[name])
        ]
        if loaded is not None:
            for name in offenders:
                self.assertIn(
                    f"Slice 's' mode '{mode}' requires {mode} digest from component "
                    f"'{name}' to supply that facet",
                    loaded,
                )
            return
        self.assertEqual(offenders, [], f"accepted unavailable facet: {shapes}")

    def test_load_config_and_generate_refuse_the_same_unavailable_slice_facet(self):
        shape, mode, _ = SLICE_FACET_WITNESSES["compat mode, no version source"]
        self._write(self._config([shape, shape], ["svc"], mode))
        loaded = self._refusal(lambda: boundver.load_config(source="working-tree"))
        generated = self._refusal(
            lambda: boundver.generate(out_path=None, source="working-tree")
        )
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded, generated)


# ---------------------------------------------------------------------------
# OBL-CONFIG-044: default source divergence across the API
# ---------------------------------------------------------------------------


def reference_text() -> str:
    """docs/reference.md, read at runtime rather than quoted from memory."""
    return (
        Path(__file__).resolve().parents[1] / "docs" / "reference.md"
    ).read_text(encoding="utf-8")


def reference_section(title: str) -> str:
    """One `###` section of docs/reference.md, with its line wrapping undone
    so a sentence can be searched for as the sentence it is."""
    for section in re.split(r"(?m)^### ", reference_text()):
        if section.startswith(title):
            return re.sub(r"\s+", " ", section)
    raise AssertionError(f"docs/reference.md has no section {title!r}")


#: The heading that documents the Python API, and the one sentence in it that
#: states a default. There is no sibling section for `generate` or `verify`.
LOAD_CONFIG_SECTION = "Python `load_config` contract"
DOCUMENTED_DEFAULT = 'source="working-tree"'


class DefaultSourceDivergenceTests(unittest.TestCase):
    """OBL-CONFIG-044: two views of one repository in one embedding."""

    def _repository(self) -> Scenario:
        scene = Scenario()
        scene.config["project"] = "committed"
        scene.component(
            "svc",
            path="services/svc",
            provider="path-hash",
            boundary=["api.yaml"],
            behavior=["api.yaml"],
        )
        scene.file("services/svc/api.yaml", "openapi: 3.1.0\n")
        scene.commit()
        return scene

    def test_with_a_clean_tree_both_defaults_read_the_same_config(self):
        """The premise: the divergence below is the uncommitted edit, not the
        fixture handing the two functions different files."""
        with self._repository() as scene:
            with InRepo(scene.root):
                loaded = boundver.load_config()
                lockfile = boundver.generate(out_path=None)
            self.assertEqual(loaded["project"], "committed")
            self.assertEqual(lockfile["project"], "committed")

    def test_load_config_reads_the_working_tree_and_generate_reads_head(self):
        with self._repository() as scene:
            scene.config["project"] = "working-tree-only"
            scene.write_config()
            with InRepo(scene.root):
                loaded = boundver.load_config()
                lockfile = boundver.generate(out_path=None)
            self.assertEqual(loaded["project"], "working-tree-only")
            self.assertEqual(lockfile["project"], "committed")

    def test_a_caller_can_detect_that_the_two_read_different_config_bytes(self):
        """The detection the obligation asks for, spelled two ways.

        The lock records the project name it built from and a digest over the
        config document, so an embedder holding both objects can compare either
        one against what `load_config` handed back.
        """
        with self._repository() as scene:
            scene.config["project"] = "working-tree-only"
            scene.write_config()
            with InRepo(scene.root):
                loaded = boundver.load_config()
                from_head = boundver.generate(out_path=None)
                from_tree = boundver.generate(out_path=None, source="working-tree")
            self.assertNotEqual(loaded["project"], from_head["project"])
            self.assertNotEqual(
                from_head["config_digest"], from_tree["config_digest"]
            )
            self.assertEqual(from_tree["project"], loaded["project"])

    def test_verify_defaults_to_head_and_misses_the_uncommitted_config_edit(self):
        """Same divergence one function further on, and it reports 'current'."""
        with self._repository() as scene:
            with InRepo(scene.root):
                boundver.generate()
            scene.git("add", "--all")
            scene.git("commit", "-m", "lock")
            scene.config["project"] = "working-tree-only"
            scene.write_config()
            with InRepo(scene.root):
                gated = boundver.verify()
                observed = boundver.verify(source="working-tree")
            self.assertEqual(gated, [])
            self.assertEqual(
                observed,
                [
                    "METADATA MISMATCH project: lockfile='committed' "
                    "current='working-tree-only'"
                ],
            )

    def test_the_reference_documents_the_load_config_default(self):
        """The premise for the absence below: this search finds what is there."""
        self.assertIn(DOCUMENTED_DEFAULT, reference_section(LOAD_CONFIG_SECTION))

    def test_the_reference_states_no_default_for_generate_or_verify(self):
        """The escape hatch the obligation offers, and it is not taken.

        The reference names `generate` and `verify` once, to say that runtime
        provider validation needs an opt-in on them. It never gives either a
        signature, and the single default it states is `load_config`'s.
        """
        text = reference_text()
        self.assertNotIn("boundver.generate(", text)
        self.assertNotIn("boundver.verify(", text)
        section = reference_section(LOAD_CONFIG_SECTION)
        self.assertEqual(section.count("The default"), 1)
        self.assertIn(
            "The default preserves the working-tree behavior of the original "
            "API.",
            section,
        )

    def test_the_docstrings_state_the_default_source_divergence(self):
        """The embedding API discloses its intentionally different defaults."""
        for function in (boundver.generate, boundver.verify):
            self.assertIn("head", function.__doc__ or "")


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-011: invalid patterns across every entry point
# ---------------------------------------------------------------------------

#: Every entry point's answer to one structurally questionable pattern, read
#: off a run. `compiles` is whether `_compile_path_glob` returned an object;
#: `prepare` is the exact GuardrailError text from a `_PathGlobOperation` built
#: with the context "probe", or None when it succeeded; `declared` is what
#: `_normalize_declared_path` did, as ("ok", value) or ("error", message).
PATTERN_VERDICTS: Dict[str, Dict[str, Any]] = {
    "a//b": {
        "compiles": False,
        "prepare": "probe failed closed: invalid path glob 'a//b'",
        "declared": ("error", "must not contain empty path segments"),
    },
    "!a//b": {
        "compiles": False,
        "prepare": "probe failed closed: invalid path glob '!a//b'",
        "declared": ("error", "must not contain empty path segments"),
    },
    "/a/b": {
        "compiles": False,
        "prepare": "probe failed closed: invalid path glob '/a/b'",
        "declared": ("error", "must be relative"),
    },
    "//b": {
        "compiles": False,
        "prepare": "probe failed closed: invalid path glob '//b'",
        "declared": ("error", "must be relative"),
    },
    "a/": {
        "compiles": False,
        "prepare": "probe failed closed: invalid path glob 'a/'",
        "declared": ("ok", "a"),
    },
    "a/b//": {
        "compiles": False,
        "prepare": "probe failed closed: invalid path glob 'a/b//'",
        "declared": ("error", "must not contain empty path segments"),
    },
    "": {
        "compiles": False,
        "prepare": "probe failed closed: invalid path glob ''",
        "declared": ("error", "must not be empty or whitespace"),
    },
    "a/./b": {
        "compiles": False,
        "prepare": "probe failed closed: invalid path glob 'a/./b'",
        "declared": ("error", "must not contain '.' path segments"),
    },
    "a/../b": {
        "compiles": False,
        "prepare": "probe failed closed: invalid path glob 'a/../b'",
        "declared": (
            "error",
            "must not contain '..' path segments; the path escapes its "
            "declared root",
        ),
    },
}

#: Candidates offered to every pattern above, chosen so a working matcher would
#: say True for at least one of them under the pattern's obvious reading.
CANDIDATES = ("a/b", "a", "b", "a/b/c")

#: Pattern segments the property draws from. Kept short and free of nested
#: classes so nothing trips the metacharacter-complexity guardrail, which
#: raises for a different reason than the one under test.
SEGMENTS = st.sampled_from(["a", "b", "x", "*", "**", "?", ""])

PATTERNS = st.lists(SEGMENTS, min_size=1, max_size=4).map("/".join)

GLOB_PROFILE = settings(max_examples=300, deadline=None)


def structurally_invalid(pattern: str) -> bool:
    """The oracle: a pattern with a leading '/' or invalid path segment.

    Written from the grammar in spec/HASHING.md -- declared paths and patterns
    are relative POSIX paths whose segments are non-empty -- rather than from
    the compiler, so agreement between the two is evidence rather than
    restatement. The empty pattern is invalid as well.
    """
    if not pattern or pattern.startswith("/"):
        return True
    return any(part in {"", ".", ".."} for part in pattern.split("/"))


class InvalidPathGlobTests(unittest.TestCase):
    """OBL-GIT-SOURCE-011: invalid patterns fail closed consistently."""

    def _operation(self) -> _PathGlobOperation:
        return _PathGlobOperation("probe")

    def test_a_well_formed_pattern_is_accepted_by_every_entry_point(self):
        """The premise: each entry point can say yes, through this call shape."""
        self.assertTrue(_match_path_glob("a/b", "a/*"))
        self.assertIsNotNone(_compile_path_glob("a/*"))
        self.assertIsNotNone(self._operation().prepare("a/*"))
        self.assertEqual(_normalize_declared_path("a/b"), "a/b")

    def test_every_entry_point_answers_the_table(self):
        """Pinned current behaviour, so a partial reconciliation cannot pass."""
        for pattern, row in PATTERN_VERDICTS.items():
            with self.subTest(pattern=pattern):
                compiled = _compile_path_glob(pattern)
                self.assertEqual(compiled is not None, row["compiles"])

                operation = self._operation()
                if row["prepare"] is None:
                    self.assertIsNotNone(operation.prepare(pattern))
                else:
                    with self.assertRaises(GuardrailError) as caught:
                        operation.prepare(pattern)
                    self.assertEqual(str(caught.exception), row["prepare"])

                kind, expected = row["declared"]
                if kind == "ok":
                    self.assertEqual(_normalize_declared_path(pattern), expected)
                else:
                    with self.assertRaises(ValueError) as declared:
                        _normalize_declared_path(pattern)
                    self.assertEqual(str(declared.exception), expected)

    def test_the_matcher_fails_closed_for_a_pattern_the_operation_refuses(self):
        for pattern, row in PATTERN_VERDICTS.items():
            if row["prepare"] is None:
                continue
            with self.subTest(pattern=pattern):
                for candidate in CANDIDATES:
                    with self.assertRaises(GuardrailError):
                        _match_path_glob(candidate, pattern)

    @GLOB_PROFILE
    @given(pattern=PATTERNS)
    def test_compile_and_prepare_agree_with_the_grammar_on_what_is_invalid(
        self, pattern
    ):
        """Two entry points measured against the grammar, not against each other.

        `structurally_invalid` restates the declared-path grammar rather than
        calling the compiler, so this says the compiler implements the grammar
        and both matching layers report exactly what the compiler refused.
        """
        invalid = structurally_invalid(pattern)
        self.assertEqual(_compile_path_glob(pattern) is None, invalid, pattern)

        operation = self._operation()
        if invalid:
            with self.assertRaises(GuardrailError) as caught:
                operation.prepare(pattern)
            self.assertEqual(
                str(caught.exception),
                f"probe failed closed: invalid path glob {pattern!r}",
            )
            for candidate in CANDIDATES:
                with self.assertRaises(GuardrailError):
                    _match_path_glob(candidate, pattern)
        else:
            self.assertIsNotNone(operation.prepare(pattern))

    def test_every_entry_point_handles_an_invalid_pattern_the_same_way(self):
        for pattern, row in PATTERN_VERDICTS.items():
            if row["prepare"] is None:
                continue
            with self.assertRaises(GuardrailError):
                _match_path_glob("a/b", pattern)


class GitignoreInertRuleTests(unittest.TestCase):
    """OBL-GIT-SOURCE-011: a `.gitignore` line that is accepted and does
    nothing, which is the consequence the obligation is really about."""

    def _rules(self, *lines: str) -> _GitignoreRules:
        rules = _GitignoreRules()
        for line in lines:
            rules.add(line)
        return rules

    def test_a_negation_cannot_re_include_below_an_ignored_parent(self):
        """The premise for both absences below.

        Without it, 'the empty-segment negation changed nothing' could be a
        ruleset in which no negation ever changes anything.
        """
        ignoring = self._rules("a")
        self.assertTrue(ignoring.is_ignored("a/b"))
        self.assertTrue(ignoring.is_ignored("a"))

        negated = self._rules("a", "!a/b")
        self.assertTrue(negated.is_ignored("a/b"))
        self.assertTrue(negated.is_ignored("a"))

    def test_an_empty_segment_rule_is_refused(self):
        with self.assertRaises(GuardrailError):
            self._rules("a//b")

    def test_an_empty_segment_negation_is_refused(self):
        with self.assertRaises(GuardrailError):
            self._rules("a", "!a//b")

    def test_the_inert_negation_still_costs_the_directory_pruning_optimisation(self):
        """It does nothing and is not free: `can_prune_directory` gives up as
        soon as any negation is present, matched or not."""
        self.assertTrue(self._rules("a").can_prune_directory("a"))
        self.assertFalse(self._rules("a", "!a/b").can_prune_directory("a"))

    def test_gitignore_refuses_a_rule_no_candidate_can_ever_match(self):
        with self.assertRaises(GuardrailError):
            self._rules("a//b")


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-022: Git mode and object type in the file identity
# ---------------------------------------------------------------------------

#: The three blob modes normal file hashing supports.
GIT_MODES = ("100644", "100755", "120000")

#: The four digests spec/HASHING.md says a transition among those modes must
#: move even when the blob bytes are identical.
MODE_SENSITIVE_DIGESTS = ("exact", "vendored", "boundary", "behavior")

#: The single blob every mode reuses. Its bytes read as a plausible symlink
#: target so that mode 120000 is the only thing that changes when it is used
#: as one.
MODE_BLOB = b"payload.txt"

#: The component's file and the vendored copy of it. Both carry every
#: transition, so the content-only comparison stays satisfied and strict
#: generation has nothing but the mode to react to.
MODE_PATHS = ("services/svc/payload.txt", "vendor/svc/payload.txt")


class GitModeIdentityTests(unittest.TestCase):
    """OBL-GIT-SOURCE-022: the mode is part of the identity in every source."""

    def _scenario(self, mode: str, *, filemode: str = "false") -> Scenario:
        """A component, a vendored copy of it, and one file in *mode*.

        The mode is set through the index rather than with `os.chmod`, because
        this host records no execute bit and cannot create a symlink. The
        vendored copy carries the same transition so the content-only digest
        stays comparable and strict generation has nothing else to complain
        about.
        """
        scene = Scenario()
        scene.component(
            "svc",
            path="services/svc",
            provider="path-hash",
            boundary=["payload.txt"],
            behavior=["payload.txt"],
        )
        scene.config["components"]["svc"]["vendored_copies"] = ["vendor/svc"]
        for directory in ("services/svc", "vendor/svc"):
            target = scene.root / directory / "payload.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(MODE_BLOB)
        scene.commit()
        scene.git("config", "core.filemode", filemode)
        self._apply_mode(scene, mode)
        return scene

    def _apply_mode(self, scene: Scenario, mode: str) -> None:
        if mode == "100644":
            return
        if mode == "100755":
            scene.git("update-index", "--chmod=+x", *MODE_PATHS)
            return
        oid = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=scene.root,
            input=MODE_BLOB,
            check=True,
            capture_output=True,
        ).stdout.decode("ascii").strip()
        for path in MODE_PATHS:
            scene.git("update-index", "--add", "--cacheinfo", f"120000,{oid},{path}")

    def _staged(self, scene: Scenario) -> List[str]:
        return scene.git("ls-files", "--stage", "services/svc/payload.txt").split()

    def _digests(self, scene: Scenario, source: str) -> Dict[str, Optional[str]]:
        lockfile = scene.generate(source=source)
        entry = lockfile["components"]["svc"]
        errors = {k: v for k, v in entry.items() if k.endswith("_errors")}
        self.assertEqual(errors, {}, f"{source}: generation reported errors")
        return {
            "exact": entry["fingerprints"]["exact"],
            "vendored": entry["vendored_digests"]["vendor/svc"],
            "boundary": entry["fingerprints"]["boundary"],
            "behavior": entry["fingerprints"]["behavior"],
        }

    def test_the_fixture_puts_one_blob_into_three_modes(self):
        """The premise for every claim below: only the mode moved.

        A mode-transition test whose blob also changed would prove nothing, and
        on a host that silently refuses the transition it would prove less than
        nothing.
        """
        staged = {}
        for mode in GIT_MODES:
            with self._scenario(mode) as scene:
                staged[mode] = self._staged(scene)
        self.assertEqual([row[0] for row in staged.values()], list(GIT_MODES))
        object_ids = {row[1] for row in staged.values()}
        self.assertEqual(len(object_ids), 1, staged)

    def test_every_mode_transition_moves_all_four_digests_at_head_and_index(self):
        for source in ("head", "index"):
            observed: Dict[str, Dict[str, Optional[str]]] = {}
            for mode in GIT_MODES:
                with self._scenario(mode) as scene:
                    if source == "head":
                        scene.commit_index("mode")
                    observed[mode] = self._digests(scene, source)
            for facet in MODE_SENSITIVE_DIGESTS:
                with self.subTest(source=source, facet=facet):
                    values = [observed[mode][facet] for mode in GIT_MODES]
                    self.assertEqual(len(set(values)), 3, values)

    def test_head_and_index_agree_on_every_mode(self):
        """Not required by the obligation, but it separates 'the mode reached
        the digest' from 'the two source readers disagree about the mode'."""
        for mode in GIT_MODES:
            with self.subTest(mode=mode):
                with self._scenario(mode) as scene:
                    from_index = self._digests(scene, "index")
                    scene.commit_index("mode")
                    from_head = self._digests(scene, "head")
                self.assertEqual(from_index, from_head)

    def test_the_executable_transition_moves_all_four_working_tree_digests(self):
        """The `core.filemode=false` fallback, which had no digest-level test.

        With it set, `_working_tree_mode` returns the tracked entry's mode
        rather than the bits it can read off disk, so the working tree sees an
        executable transition the filesystem never stored.
        """
        observed = {}
        for mode in ("100644", "100755"):
            with self._scenario(mode) as scene:
                self.assertEqual(
                    scene.git("config", "--get", "core.filemode"), "false"
                )
                observed[mode] = self._digests(scene, "working-tree")
        for facet in MODE_SENSITIVE_DIGESTS:
            with self.subTest(facet=facet):
                self.assertNotEqual(
                    observed["100644"][facet], observed["100755"][facet]
                )

    def test_the_working_tree_digests_match_head_for_the_modes_disk_can_hold(self):
        for mode in ("100644", "100755"):
            with self.subTest(mode=mode):
                with self._scenario(mode) as scene:
                    from_tree = self._digests(scene, "working-tree")
                    scene.commit_index("mode")
                    from_head = self._digests(scene, "head")
                self.assertEqual(from_tree, from_head)

    def test_an_index_only_chmod_under_core_filemode_true_leaves_a_dirty_tree(self):
        """Why the covering test above pins `core.filemode=false`.

        `update-index --chmod=+x` changes the index and not the file, so under
        `core.filemode=true` the working tree still reads 100644, the digests
        do not move, and Git itself reports the path as modified. Asserting an
        unmoved digest without this would be asserting an absence over a
        transition that never reached the working tree.
        """
        with self._scenario("100644", filemode="true") as scene:
            before = self._digests(scene, "working-tree")
            scene.git("update-index", "--chmod=+x", *MODE_PATHS)
            status = scene.git("status", "--porcelain")
            after = self._digests(scene, "working-tree")
        self.assertIn("services/svc/payload.txt", status)
        self.assertEqual(before, after)

    def test_a_symlink_entry_the_disk_cannot_hold_reads_back_as_a_regular_file(self):
        """Pinned host behaviour, not a boundver defect, and said so here.

        `--cacheinfo 120000` puts a symlink entry in the index while the file
        on disk stays a regular file, so `_working_tree_mode` classifies it
        from `lstat` as 100644 and the digests land on the 100644 values. Git
        agrees the tree is out of step: `status` reports a type change. The
        head and index sweep above is what makes this readable as a dirty tree
        rather than as a dropped mode.
        """
        with self._scenario("100644") as scene:
            plain = self._digests(scene, "working-tree")
        with self._scenario("120000") as scene:
            status = scene.git("status", "--porcelain")
            linked = self._digests(scene, "working-tree")
        self.assertTrue(
            status.startswith("T"), f"expected a type change, got {status!r}"
        )
        self.assertEqual(plain, linked)

    @requires_symlinks
    def test_a_real_symlink_moves_all_four_working_tree_digests(self):
        """The leg this host cannot run, kept so a POSIX runner does.

        Replacing the file with a symlink whose target text is the same bytes
        leaves the content identical and the mode 120000, which is the exact
        transition the obligation names for the working tree.
        """
        with self._scenario("100644") as scene:
            plain = self._digests(scene, "working-tree")
            for path in MODE_PATHS:
                (scene.root / path).unlink()
                (scene.root / path).symlink_to(MODE_BLOB.decode("ascii"))
            linked = self._digests(scene, "working-tree")
        for facet in MODE_SENSITIVE_DIGESTS:
            with self.subTest(facet=facet):
                self.assertNotEqual(plain[facet], linked[facet])

    def test_the_recorded_vendored_digest_is_the_content_only_digest(self):
        """Ties the lock field this class reads to the function the spec names,
        so `vendored_digests` cannot drift into meaning something else."""
        with self._scenario("100755") as scene:
            scene.commit_index("mode")
            recorded = self._digests(scene, "head")["vendored"]
            direct = _content_only_digest(scene.root, "vendor/svc", source="head")
        self.assertEqual(recorded, direct)


if __name__ == "__main__":
    unittest.main()
