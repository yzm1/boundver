"""Two consumer-graph traversals, five declared bounds, and a discovery ceiling
that counts the wrong things.

The consumer graph is the part of boundver that other machines act on. A slice
membership goes into the lock and becomes a CI cache key; an `AFFECTED
CONSUMERS` line becomes a fan-out matrix; a review's `consumer_impact` becomes
the list of teams a pull request pages. The obligations gathered here all say
the same thing in different words: that answer must not depend on anything
except the edge set. It must not depend on the order the components happen to
appear in the config file, it must not depend on whether the caller went
through `verify` or through `review`, and when the graph is too large to answer
completely the answer must be withheld rather than truncated. Three of the four
config obligations turned out to hold, which is worth recording precisely
because the register only records what was checked; the fourth is a real
divergence that validation currently hides.

Getting the comparison to mean anything took more than calling both functions.
`affected_consumer_groups` returns sorted lists of strings and
`_review._walk_consumer_graph` returns sets plus an edge triple set, so the
shapes had to be reconciled before they could disagree. The comparison's
premise test injects a known disagreement so normalization cannot make the
harness vacuous. OBL-CONFIG-007 also passes a malformed self-edge directly to
both helpers: validation refuses that graph, and the helpers independently
exclude the declaring component from its own impact report.

The bounds obligation needed a repository rather than a dict, because its
second clause is about commands and not about `validate_config`. A config that
trips a bound has to be committed on top of a range whose two endpoints are
both reconciled, or `review` fails for the wrong reason and the test proves
nothing. The fixture therefore builds one repository with two reconciled
commits, and each bound case commits its over-large config, runs the four
commands, and resets. Its premise is the same repository one commit earlier,
where all three commands do emit a closure - `generate` prints `Declared
consumer edges (recorded in lock):`, `verify` reports `AFFECTED CONSUMERS api:
team-x, web`, `review` fills `consumer_impact` - so the emptiness asserted for
the over-large configs is an observed absence rather than a command that never
ran.

Two things about that table turned out to need defending. An exit status of 2
attributes nothing: an ordinary invalid config, a single unknown consumer
name, produces every refusal the bound rows assert, and the published schema
bounds `components`, `consumers`, `external_consumers` and identifier length
on its own, so four of the rows would keep passing with every bound check in
`_config.py` deleted. Each row therefore asserts the diagnostic only its own
bound can produce, and a control row that is invalid without any bound asserts
the absence of all of them. Separately, the unchanged lock a refused `generate`
leaves behind means nothing until something shows the file moves: `generate`
reads HEAD by default, so the closure premise drifts a working-tree file and
leaves `boundary.lock.json` byte-identical even when the command is allowed to
run. A committed config edit does move it, and that is now its own premise.

The two discovery obligations are about a guardrail and a promise, and both
fail. `discover_components` accepts `max_discovery_manifests` as a keyword, so
the ceiling can be lowered to single digits and tested with four files instead
of fifty thousand; the hardcoded ignore-name set, the manifest-name list and
the conventional root names are all read out of the function's own source with
`ast`, so a name added to any of the three is covered here without an edit.
The roots needed a second reader, because they are written inline as a `for`
loop's iterator rather than assigned to anything. The index-backed branch counts every tracked
`vendor/**/package.json` toward a ceiling it will then skip, while the
filesystem fallback never descends into those directories, so the identical
file tree is refused as a Git repository and accepted as a plain directory.
And `init --discover` writes, exits 0, and hands the user a config that
`validate-config` immediately rejects, for two shapes the obligation names by
hand: a tracked regular file called `src` at the root, and a manifest that is
in the index while its directory is gone from the working tree.

Covers OBL-CONFIG-007, OBL-CONFIG-015, OBL-CONFIG-016, OBL-CONFIG-017,
OBL-GIT-SOURCE-149 and OBL-GIT-SOURCE-150.
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
from unittest.mock import patch

from hypothesis import HealthCheck, Phase, given, settings
from hypothesis import strategies as st

import boundver._config as config_module
from boundver._config import validate_config
from boundver._config_contract import (
    MAX_CONSUMER_GRAPH_ITEMS,
    MAX_CONSUMER_IDENTIFIER_CHARS,
)
from boundver._consumer_graph import (
    affected_consumer_groups,
    consumer_closure,
    resolve_slice_components,
)
from boundver._discovery import discover_components
from boundver._review import _ReviewWorkBudget, _walk_consumer_graph
from boundver._utils import ConfigError, GuardrailError

from tests._parity import run_cli
from tests._scenarios import Scenario


# --------------------------------------------------------------------------
# Surfaces read out of the code rather than listed here.
# --------------------------------------------------------------------------


def _local_literal(function: Any, name: str) -> Any:
    """Return the literal a function assigns to a local *name*.

    `discover_components` keeps its ignore-name set and its manifest table as
    function locals, so neither can be imported. Reading them from the parsed
    source keeps this file's tables derived: a directory name added to the
    ignore set, or a manifest added to the spec table, is enumerated here on
    the next run instead of being silently left out.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{function.__name__} has no literal assignment to {name}")


def _loop_literal(function: Any, name: str) -> Any:
    """Return the literal a function iterates in `for <name> in ...:`.

    The conventional roots are not assigned anywhere; they are written inline
    as the iterator of a `for` loop, which `_local_literal` walks straight
    past. Reading them here keeps that surface derived too, so a fourth root
    added to the loop is exercised by the pinning table and the divergence
    below on the next run rather than being silently left uncovered.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            return ast.literal_eval(node.iter)
    raise AssertionError(f"{function.__name__} has no `for {name} in <literal>:`")


#: Every directory name discovery refuses to descend into, read from
#: `discover_components` itself.
IGNORED_DIRECTORY_NAMES = tuple(
    sorted(_local_literal(discover_components, "_ignored_dirs"))
)

#: (manifest filename, version field) exactly as discovery declares it.
MANIFEST_SPECS = tuple(_local_literal(discover_components, "manifest_specs"))

MANIFEST_NAMES = tuple(name for name, _field in MANIFEST_SPECS)

#: Git will not track a path under `.git`, so that one ignore name cannot be
#: given a tracked manifest. `DiscoveryCeilingDenominatorTests` proves that is
#: the only exception rather than assuming it.
UNTRACKABLE_IGNORED_NAME = ".git"

TRACKABLE_IGNORED_NAMES = tuple(
    name for name in IGNORED_DIRECTORY_NAMES if name != UNTRACKABLE_IGNORED_NAME
)

#: A body for every manifest discovery knows about. A missing key here fails
#: `test_every_declared_manifest_kind_has_a_fixture_body`, so a new manifest
#: spec cannot silently drop out of the generated repository shapes.
MANIFEST_BODIES = {
    "package.json": json.dumps({"name": "p", "version": "1.0.0"}, indent=2) + "\n",
    "pyproject.toml": '[project]\nname = "p"\nversion = "1.0.0"\n',
    "Cargo.toml": '[package]\nname = "p"\nversion = "1.0.0"\n',
    "go.mod": "module example.com/p\n\ngo 1.21\n",
}

PACKAGE_JSON = MANIFEST_BODIES["package.json"]

#: Every bare name the root-manifest remap will accept as a component root,
#: read out of `discover_components`' conventional loop rather than copied.
CONVENTIONAL_ROOTS = tuple(_loop_literal(discover_components, "conventional"))

#: Component directory names for generated repositories. None of them is a
#: conventional root, which
#: `test_no_generated_component_directory_is_a_conventional_root` enforces
#: rather than assuming, so the remap only fires when a shape asks for it.
GENERATED_COMPONENT_DIRECTORIES = ("svc", "web", "tools", "api")

#: The exact warning the non-Git fallback prints before approximating.
FALLBACK_WARNING = (
    "WARNING: component discovery is using a bounded filesystem "
    "approximation because Git repository semantics are unavailable"
)


# --------------------------------------------------------------------------
# Consumer-graph helpers.
# --------------------------------------------------------------------------


def _component(path: str, consumers=(), external_consumers=()) -> Dict[str, Any]:
    return {
        "path": path,
        "boundary": {"provider": "leaf", "paths": []},
        "consumers": list(consumers),
        "external_consumers": list(external_consumers),
    }


def _graph(edges: Dict[str, Tuple[List[str], List[str]]]) -> Dict[str, Any]:
    return {
        name: _component(name, consumers, external)
        for name, (consumers, external) in edges.items()
    }


#: A malformed graph used to verify that both traversals defend against a
#: self-edge even when a caller bypasses `validate_config`.
SELF_CONSUMING_GRAPH = _graph({"a": (["a", "b"], []), "b": ([], [])})


def _answers(components: Dict[str, Any], explicit_members: List[str]) -> str:
    """Every output OBL-CONFIG-015 covers, serialised for a byte comparison.

    Seeds are iterated in sorted order on purpose: the obligation is about each
    function's return value, not about the order this helper happens to call
    them in, and an unsorted iteration would report a difference that belongs
    to the harness.
    """
    seeds = sorted(components)
    return json.dumps(
        {
            "closure": consumer_closure(components, seeds),
            "closure_with_seeds": consumer_closure(
                components, seeds, include_seeds=True
            ),
            "direct": {
                seed: affected_consumer_groups(components, seed) for seed in seeds
            },
            "transitive": {
                seed: affected_consumer_groups(components, seed, transitive=True)
                for seed in seeds
            },
            "closure_slices": {
                seed: resolve_slice_components({"closure_of": seed}, components)
                for seed in seeds
            },
            "explicit_slice": resolve_slice_components(
                {"components": explicit_members}, components
            ),
        }
    )


class Disagreement(NamedTuple):
    """One place the two traversals answered the same question differently."""

    seed: str
    transitive: bool
    field: str
    from_consumer_graph: List[str]
    from_review: List[str]


def _traversal_disagreements(components: Dict[str, Any]) -> List[Disagreement]:
    """Compare `affected_consumer_groups` with `_walk_consumer_graph`.

    `_walk_consumer_graph` returns sets and an edge triple set; the graph
    helper returns sorted lists under two keys. Only the two sets the review
    text and the verify diagnostics both render are compared, because the edge
    triples have no counterpart on the other side.
    """
    rows: List[Disagreement] = []
    for seed in sorted(components):
        for transitive in (False, True):
            groups = affected_consumer_groups(
                components, seed, transitive=transitive
            )
            internal, external, _edges = _walk_consumer_graph(
                components,
                seed,
                transitive=transitive,
                budget=_ReviewWorkBudget(),
            )
            pairs = (
                ("components", set(groups["components"]), internal),
                ("external_consumers", set(groups["external_consumers"]), external),
            )
            for field, from_graph, from_review in pairs:
                if from_graph != from_review:
                    rows.append(
                        Disagreement(
                            seed,
                            transitive,
                            field,
                            sorted(from_graph),
                            sorted(from_review),
                        )
                    )
    return rows


def _materialise_components(root: Path, components: Dict[str, Any]) -> None:
    for entry in components.values():
        (root / entry["path"]).mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Discovery helpers.
# --------------------------------------------------------------------------


class Outcome(NamedTuple):
    """What one `discover_components` call did, refusal included."""

    components: Optional[Dict[str, dict]]
    refusal: Optional[str]
    warnings: str

    @property
    def refused(self) -> bool:
        return self.refusal is not None


def _discover(root: Path, **kwargs: Any) -> Outcome:
    """Run discovery and capture the fallback warning instead of printing it."""
    stream = io.StringIO()
    with redirect_stderr(stream):
        try:
            found = discover_components(root, **kwargs)
        except (ConfigError, GuardrailError) as exc:
            return Outcome(None, str(exc), stream.getvalue())
    return Outcome(found, None, stream.getvalue())


def _write_files(root: Path, files: Dict[str, str]) -> None:
    for name, body in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")


def _track_everything(root: Path) -> None:
    """Stage the whole tree, ignore files and all.

    `--force` matters: a developer's global excludes file commonly lists
    `node_modules` and `.venv`, and without it the fixture would silently fail
    to track the manifests whose treatment is the whole question.
    """
    subprocess.run(
        ["git", "add", "--all", "--force"],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _tracked_paths(root: Path) -> List[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=root, check=True, capture_output=True, text=True
    )
    return result.stdout.split()


def _manifest_tree(visible: List[str], hidden: List[str]) -> Dict[str, str]:
    """One manifest per visible directory, one per ignored directory name."""
    files = {f"{name}/package.json": PACKAGE_JSON for name in visible}
    files.update({f"{name}/pkg/package.json": PACKAGE_JSON for name in hidden})
    return files


def _init_style_config(root: Path, discovered: Dict[str, dict]) -> Dict[str, Any]:
    """The document `init --discover` writes, discovered map included verbatim.

    `_cmd_init` performs no validation and no filtering between discovery and
    `_write_config_atomic`, so reproducing its wrapper here is what makes
    `validate_config` the same question the user's next command asks.
    """
    return {
        "project": root.name,
        "defaults": {"compat_mode": "major"},
        "components": discovered,
    }


DISCOVERY_PROFILE = settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
    # Each example builds a Git repository, and shrinking a failure that is
    # already minimal would rebuild several more for a message no
    # expectedFailure ever prints.
    phases=[Phase.explicit, Phase.reuse, Phase.generate],
)

GRAPH_PROFILE = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

VALIDATED_GRAPH_PROFILE = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

_GRAPH_NAMES = ("a", "b", "c", "d", "e")
_EXTERNAL_LABELS = ("ext-one", "ext-two", "ext-three")


@st.composite
def _valid_graphs(draw) -> Dict[str, Any]:
    """Graphs shaped the way `validate_config` accepts them.

    Every constraint here is one validation enforces: no self-edge, no
    duplicate, every internal consumer configured, and no external label that
    collides with a component name. The tests that use this strategy call
    `validate_config` on the result rather than trusting the list, so a
    constraint that drifts out of step shows up as a failure here.
    """
    present = sorted(
        draw(
            st.lists(
                st.sampled_from(_GRAPH_NAMES),
                min_size=1,
                max_size=len(_GRAPH_NAMES),
                unique=True,
            )
        )
    )
    components: Dict[str, Any] = {}
    for name in present:
        others = [candidate for candidate in present if candidate != name]
        consumers = draw(
            st.lists(
                st.sampled_from(others) if others else st.nothing(),
                max_size=len(others),
                unique=True,
            )
        )
        external = draw(
            st.lists(
                st.sampled_from(_EXTERNAL_LABELS),
                max_size=len(_EXTERNAL_LABELS),
                unique=True,
            )
        )
        components[name] = _component(name, consumers, external)
    return components


@st.composite
def _repository_shapes(draw, *, defective: bool) -> Dict[str, Any]:
    """A repository description discovery will be asked to turn into a config.

    With *defective* false the shape is an ordinary multi-package repository.
    With it true exactly one of the two shapes OBL-GIT-SOURCE-150 names by hand
    is always present, so the property has a witness on its first example
    rather than depending on the generator to find one.
    """
    directories = draw(
        st.lists(
            st.sampled_from(GENERATED_COMPONENT_DIRECTORIES),
            min_size=1,
            max_size=3,
            unique=True,
        )
    )
    manifests = [draw(st.sampled_from(MANIFEST_NAMES)) for _ in directories]
    conventional: Optional[str] = None
    deleted: List[str] = []
    if defective:
        defect = draw(st.sampled_from(("conventional-file", "index-only", "both")))
        if defect in ("conventional-file", "both"):
            conventional = draw(st.sampled_from(CONVENTIONAL_ROOTS))
        if defect in ("index-only", "both"):
            deleted = draw(
                st.lists(
                    st.sampled_from(directories),
                    min_size=1,
                    max_size=len(directories),
                    unique=True,
                )
            )
    return {
        "directories": directories,
        "manifests": manifests,
        "conventional": conventional,
        "deleted": deleted,
    }


def _build_shape(scene: Scenario, shape: Dict[str, Any]) -> None:
    files: Dict[str, str] = {}
    for directory, manifest in zip(shape["directories"], shape["manifests"]):
        files[f"{directory}/{manifest}"] = MANIFEST_BODIES[manifest]
    if shape["conventional"] is not None:
        files["package.json"] = PACKAGE_JSON
        files[shape["conventional"]] = "this is a regular file, not a directory\n"
    _write_files(scene.root, files)
    _track_everything(scene.root)
    for directory in shape["deleted"]:
        shutil.rmtree(scene.root / directory)


# --------------------------------------------------------------------------
# OBL-CONFIG-007
# --------------------------------------------------------------------------


class SelfConsumingComponentTests(unittest.TestCase):
    """OBL-CONFIG-007: a self-edge must be refused, and must not survive."""

    SELF_EDGE_ERROR = "Component 'a' cannot consume its own boundary"

    def test_validation_refuses_a_component_that_consumes_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _materialise_components(root, SELF_CONSUMING_GRAPH)
            errors = validate_config(
                {"project": "p", "components": SELF_CONSUMING_GRAPH}, root
            )
        self.assertEqual(errors, [self.SELF_EDGE_ERROR])

    def test_the_same_graph_without_the_self_edge_validates_clean(self):
        """The premise: the refusal above is the self-edge and nothing else."""
        components = _graph({"a": (["b"], []), "b": ([], [])})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _materialise_components(root, components)
            errors = validate_config({"project": "p", "components": components}, root)
        self.assertEqual(errors, [])

    def test_direct_impact_omits_the_component_that_declared_the_self_edge(self):
        """Direct impact applies the same self-exclusion as graph traversal."""
        groups = affected_consumer_groups(SELF_CONSUMING_GRAPH, "a")
        self.assertNotIn("a", groups["components"])

    def test_direct_impact_still_lists_the_real_consumer(self):
        groups = affected_consumer_groups(SELF_CONSUMING_GRAPH, "a")
        self.assertEqual(
            groups, {"components": ["b"], "external_consumers": []}
        )

    def test_transitive_impact_already_excludes_the_self_edge(self):
        """Transitive mode routes through `consumer_closure`, which drops seeds."""
        groups = affected_consumer_groups(
            SELF_CONSUMING_GRAPH, "a", transitive=True
        )
        self.assertEqual(groups, {"components": ["b"], "external_consumers": []})

    def test_the_review_traversal_excludes_the_self_edge_in_both_modes(self):
        for transitive in (False, True):
            with self.subTest(transitive=transitive):
                internal, external, edges = _walk_consumer_graph(
                    SELF_CONSUMING_GRAPH,
                    "a",
                    transitive=transitive,
                    budget=_ReviewWorkBudget(),
                )
                self.assertEqual(internal, {"b"})
                self.assertEqual(external, set())
                self.assertIn(("a", "a", "component"), edges)


# --------------------------------------------------------------------------
# OBL-CONFIG-015
# --------------------------------------------------------------------------


class ConsumerGraphPermutationTests(unittest.TestCase):
    """OBL-CONFIG-015: the same edge set, however it is spelled."""

    #: One logical graph written two ways. Same edges, different insertion
    #: order for the components dict and reversed consumer/external arrays.
    FORWARD = _graph(
        {
            "a": (["b", "c"], ["ext-one", "ext-two"]),
            "b": (["d"], ["ext-three"]),
            "c": (["d"], []),
            "d": ([], ["ext-one"]),
        }
    )
    REVERSED = _graph(
        {
            "d": ([], ["ext-one"]),
            "c": (["d"], []),
            "b": (["d"], ["ext-three"]),
            "a": (["c", "b"], ["ext-two", "ext-one"]),
        }
    )

    def test_the_two_spellings_really_are_different_documents(self):
        """The premise: the equality below is not comparing a graph to itself."""
        self.assertNotEqual(
            json.dumps(self.FORWARD), json.dumps(self.REVERSED)
        )
        self.assertNotEqual(list(self.FORWARD), list(self.REVERSED))
        self.assertNotEqual(
            self.FORWARD["a"]["consumers"], self.REVERSED["a"]["consumers"]
        )
        self.assertNotEqual(
            self.FORWARD["a"]["external_consumers"],
            self.REVERSED["a"]["external_consumers"],
        )

    def test_the_comparison_notices_a_graph_that_is_genuinely_different(self):
        """The premise: `_answers` is sensitive to the edge set it is given."""
        dropped = _graph(
            {
                "a": (["b"], ["ext-one", "ext-two"]),
                "b": (["d"], ["ext-three"]),
                "c": (["d"], []),
                "d": ([], ["ext-one"]),
            }
        )
        self.assertNotEqual(
            _answers(self.FORWARD, ["a", "b"]), _answers(dropped, ["a", "b"])
        )

    def test_a_reordered_config_produces_byte_identical_answers(self):
        self.assertEqual(
            _answers(self.FORWARD, ["a", "b", "c"]),
            _answers(self.REVERSED, ["c", "b", "a"]),
        )

    @GRAPH_PROFILE
    @given(components=_valid_graphs(), data=st.data())
    def test_every_permutation_of_an_arbitrary_graph_answers_identically(
        self, components, data
    ):
        """The unbounded form: any key order, any array order, same answer."""
        names = list(components)
        permuted_names = data.draw(st.permutations(names))
        permuted: Dict[str, Any] = {}
        for name in permuted_names:
            entry = dict(components[name])
            entry["consumers"] = data.draw(
                st.permutations(entry["consumers"])
            )
            entry["external_consumers"] = data.draw(
                st.permutations(entry["external_consumers"])
            )
            permuted[name] = entry
        members = sorted(names)
        self.assertEqual(
            _answers(components, members),
            _answers(permuted, data.draw(st.permutations(members))),
        )


# --------------------------------------------------------------------------
# OBL-CONFIG-016
# --------------------------------------------------------------------------


class ConsumerGraphAgreementTests(unittest.TestCase):
    """OBL-CONFIG-016: `verify`'s traversal and `review`'s must agree."""

    def test_the_comparison_reports_a_difference_when_one_exists(self):
        """Inject a known disagreement so the comparison is not vacuous."""
        original = affected_consumer_groups

        def divergent_groups(components, component_name, *, transitive=False):
            groups = original(
                components,
                component_name,
                transitive=transitive,
            )
            if component_name == "a" and not transitive:
                groups["components"] = ["a", *groups["components"]]
            return groups

        with patch(
            f"{__name__}.affected_consumer_groups",
            side_effect=divergent_groups,
        ):
            rows = _traversal_disagreements(SELF_CONSUMING_GRAPH)
        self.assertEqual(
            rows,
            [
                Disagreement(
                    seed="a",
                    transitive=False,
                    field="components",
                    from_consumer_graph=["a", "b"],
                    from_review=["b"],
                )
            ],
        )

    def test_the_two_traversals_agree_on_a_hand_written_valid_config(self):
        components = _graph(
            {
                "layer": (["service"], ["docs-site"]),
                "service": (["app"], []),
                "app": ([], ["mobile"]),
                "legacy": (["service"], ["partner"]),
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _materialise_components(root, components)
            self.assertEqual(
                validate_config({"project": "p", "components": components}, root), []
            )
        self.assertEqual(_traversal_disagreements(components), [])

    @VALIDATED_GRAPH_PROFILE
    @given(components=_valid_graphs())
    def test_the_two_traversals_agree_on_every_seed_of_any_valid_config(
        self, components
    ):
        """Every configured seed, both modes, over generated valid configs.

        `validate_config` runs inside the property rather than beside it: the
        obligation is quantified over configs that pass validation, so a
        generated graph validation would reject must fail here loudly instead
        of being compared anyway.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _materialise_components(root, components)
            errors = validate_config(
                {"project": "p", "components": components}, root
            )
        self.assertEqual(errors, [], f"generated an invalid config: {components}")
        self.assertEqual(_traversal_disagreements(components), [])


# --------------------------------------------------------------------------
# OBL-CONFIG-017
# --------------------------------------------------------------------------


def _too_many_components(config: Dict[str, Any]) -> None:
    for index in range(MAX_CONSUMER_GRAPH_ITEMS + 1):
        config["components"][f"g{index}"] = {
            "path": f"gen/{index}",
            "boundary": {"provider": "leaf", "paths": []},
        }


def _too_many_consumers(config: Dict[str, Any]) -> None:
    config["components"]["api"]["consumers"] = [
        f"c{index}" for index in range(MAX_CONSUMER_GRAPH_ITEMS + 1)
    ]


def _too_many_external_entries(config: Dict[str, Any]) -> None:
    config["components"]["api"]["external_consumers"] = [
        f"x{index}" for index in range(MAX_CONSUMER_GRAPH_ITEMS + 1)
    ]


def _too_many_external_labels(config: Dict[str, Any]) -> None:
    """Split across two components, so no single array trips its own bound."""
    half = MAX_CONSUMER_GRAPH_ITEMS // 2 + 1
    config["components"]["api"]["external_consumers"] = [
        f"x{index}" for index in range(half)
    ]
    config["components"]["web"]["external_consumers"] = [
        f"y{index}" for index in range(half)
    ]


def _too_long_a_name(config: Dict[str, Any]) -> None:
    config["components"]["n" * (MAX_CONSUMER_IDENTIFIER_CHARS + 1)] = {
        "path": "web",
        "boundary": {"provider": "leaf", "paths": []},
    }


def _too_long_a_consumer_identifier(config: Dict[str, Any]) -> None:
    config["components"]["api"]["consumers"] = [
        "n" * (MAX_CONSUMER_IDENTIFIER_CHARS + 1)
    ]


def _too_long_an_external_identifier(config: Dict[str, Any]) -> None:
    config["components"]["api"]["external_consumers"] = [
        "x" * (MAX_CONSUMER_IDENTIFIER_CHARS + 1)
    ]


def _an_unknown_consumer(config: Dict[str, Any]) -> None:
    """The control: invalid, but for a reason no bound is involved in."""
    config["components"]["api"]["consumers"] = [UNKNOWN_CONSUMER]


UNKNOWN_CONSUMER = "nope-not-configured"

#: Every consumer-graph bound the code enforces: the mutation that trips it
#: and the leading text of the diagnostic validate_config produces. The four
#: count bounds give their complete message; the three identifier-length ones
#: are followed by a bounded repr of the offending name, so only the prefix is
#: listed. OBL-CONFIG-017 names the first five by hand; the two consumer
#: identifier-length checks at _config.py:1006-1010 and 1057-1061 enforce the
#: same constant and are included so the table means what its name says.
#: The same four shapes as BOUND_CASES, built to sit exactly on a ceiling
#: rather than one past it. Each takes the ceiling as an argument so the
#: assertion can run against a patched-down limit instead of ten thousand
#: entries.
def _components_at(config: Dict[str, Any], limit: int) -> None:
    while len(config["components"]) < limit:
        index = len(config["components"])
        config["components"][f"g{index}"] = {
            "path": f"gen/{index}",
            "boundary": {"provider": "leaf", "paths": []},
        }


def _consumers_at(config: Dict[str, Any], limit: int) -> None:
    config["components"]["api"]["consumers"] = [
        f"c{index}" for index in range(limit)
    ]


def _external_entries_at(config: Dict[str, Any], limit: int) -> None:
    config["components"]["api"]["external_consumers"] = [
        f"x{index}" for index in range(limit)
    ]


def _external_labels_at(config: Dict[str, Any], limit: int) -> None:
    """Split across two components so no single array reaches its own bound."""
    half = limit // 2
    config["components"]["api"]["external_consumers"] = [
        f"x{index}" for index in range(half)
    ]
    config["components"]["web"]["external_consumers"] = [
        f"y{index}" for index in range(limit - half)
    ]


AT_LIMIT_CASES = {
    "components per config": _components_at,
    "entries in one consumers array": _consumers_at,
    "entries in one external_consumers array": _external_entries_at,
    "distinct external labels repository-wide": _external_labels_at,
}

#: Small enough to build by hand, large enough that the two halves of the
#: external-label case are both non-empty.
PATCHED_LIMIT = 4

BOUND_CASES = {
    "components per config": (
        _too_many_components,
        "Field 'components' exceeds the 10000-component consumer-graph limit",
    ),
    "entries in one consumers array": (
        _too_many_consumers,
        "Component 'api' field 'consumers' exceeds the 10000-entry "
        "consumer-graph limit",
    ),
    "entries in one external_consumers array": (
        _too_many_external_entries,
        "Component 'api' field 'external_consumers' exceeds the 10000-entry "
        "consumer-graph limit",
    ),
    "distinct external labels repository-wide": (
        _too_many_external_labels,
        "Config declares more than 10000 distinct external consumer labels; "
        "the repository-wide consumer graph must fit the machine-output contract",
    ),
    "characters in a component name": (
        _too_long_a_name,
        "Component name exceeds the 16384-character consumer-graph limit: ",
    ),
    "characters in a consumer identifier": (
        _too_long_a_consumer_identifier,
        "Component 'api' consumer identifier exceeds the 16384-character "
        "limit: ",
    ),
    "characters in an external consumer identifier": (
        _too_long_an_external_identifier,
        "Component 'api' external consumer identifier exceeds the "
        "16384-character limit: ",
    ),
}

#: The control row for the command-level table: a config that is invalid
#: without tripping any bound. Every exit status and every empty closure the
#: bound rows assert is reproduced here, which is what makes the per-row
#: diagnostic - and not the exit status - the part that attributes a refusal
#: to its bound.
CONTROL_CASE = (
    _an_unknown_consumer,
    f"Component 'api' references unknown consumer: {UNKNOWN_CONSUMER}",
)

#: What the four commands are run against: every bound, then the control. The
#: third element says whether the row is a bound, which is what decides
#: whether the row must show a bound diagnostic or must show none.
REFUSAL_CASES = {
    **{
        label: (mutate, message, True)
        for label, (mutate, message) in BOUND_CASES.items()
    },
    "an unknown consumer name (control)": (
        CONTROL_CASE[0],
        CONTROL_CASE[1],
        False,
    ),
}

#: Text that would betray a closure in a command's human-readable output.
CLOSURE_MARKERS = (
    "AFFECTED CONSUMERS",
    "Consumer impact:",
    "Affected components:",
    "Declared consumer edges (recorded in lock):",
)

EXIT_USAGE = 2
EXIT_BOUNDARY = 4
DRIFTED_CONTENT = "drifted\n"


class ConsumerGraphBoundTests(unittest.TestCase):
    """OBL-CONFIG-017: refuse the whole graph rather than truncate it."""

    scene: Scenario

    @classmethod
    def setUpClass(cls) -> None:
        """Two reconciled commits, so `review HEAD~1..HEAD` has real endpoints.

        A range review validates and reconciles both endpoints. Without a base
        commit whose lock matches its own tree, every assertion below would
        pass for the wrong reason: review would refuse the range rather than
        the config.
        """
        cls.scene = Scenario("bounds")
        cls.scene.component(
            "api",
            path="api",
            provider="path-hash",
            boundary=["*.txt"],
            consumers=["web"],
            external_consumers=["team-x"],
        )
        cls.scene.component(
            "web", path="web", provider="path-hash", boundary=["*.txt"]
        )
        cls.scene.file("api/a.txt", "a\n")
        cls.scene.file("web/b.txt", "b\n")
        cls.scene.commit("declare")
        run_cli(cls.scene.root, "generate")
        cls.scene.git("add", "--all")
        cls.scene.git("commit", "-m", "lock")
        cls.scene.file("api/a.txt", "a2\n")
        run_cli(cls.scene.root, "generate", "--source", "working-tree")
        cls.scene.git("add", "--all")
        cls.scene.git("commit", "-m", "edit and relock")
        cls.lock_bytes = (cls.scene.root / "boundary.lock.json").read_bytes()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    def setUp(self) -> None:
        self.scene.git("reset", "--hard")

    def tearDown(self) -> None:
        self.scene.git("reset", "--hard")

    def _config(self) -> Dict[str, Any]:
        return json.loads(
            (self.scene.root / "boundary.config.json").read_text(encoding="utf-8")
        )

    def _commit_invalid_config(self, mutate) -> None:
        config = self._config()
        mutate(config)
        (self.scene.root / "boundary.config.json").write_text(
            json.dumps(config, indent=2) + "\n", encoding="utf-8"
        )
        self.scene.git("add", "--all")
        self.scene.git("commit", "-m", "over-bounded graph")
        # The drift is applied after the commit so the working tree diverges
        # from the lock: without it verify has nothing to report a closure
        # about, and its empty closure would prove nothing.
        self.scene.file("api/a.txt", DRIFTED_CONTENT)

    def test_the_documented_numbers_match_the_constants_the_code_enforces(self):
        """docs/reference.md:375-378 states both ceilings in words."""
        self.assertEqual(MAX_CONSUMER_GRAPH_ITEMS, 10_000)
        self.assertEqual(MAX_CONSUMER_IDENTIFIER_CHARS, 16_384)

    def test_every_declared_bound_produces_its_own_validation_diagnostic(self):
        for label, (mutate, message) in BOUND_CASES.items():
            with self.subTest(bound=label):
                config = self._config()
                mutate(config)
                errors = validate_config(config, self.scene.root)
                matching = [
                    error for error in errors if error.startswith(message)
                ]
                self.assertEqual(
                    len(matching),
                    1,
                    f"{label}: expected exactly one {message!r} in {errors[:4]}",
                )

    def test_each_bound_accepts_a_config_that_sits_exactly_on_it(self):
        """The at-the-limit control every breach row rests on.

        Each row in BOUND_CASES builds MAX + 1 entries, so all of them keep
        refusing when the comparison is loosened by one and the suite cannot
        tell the documented ceiling from one component lower
        (MUT-B1-18). docs/reference.md promises "at most 10,000 configured
        components", so a config holding exactly the ceiling has to validate.
        The ceiling is patched down rather than built out to ten thousand.
        """
        for label, build in AT_LIMIT_CASES.items():
            with self.subTest(bound=label):
                config = self._config()
                build(config, PATCHED_LIMIT)
                with patch.object(
                    config_module, "MAX_CONSUMER_GRAPH_ITEMS", PATCHED_LIMIT
                ):
                    errors = validate_config(config, self.scene.root)
                self.assertEqual(
                    [error for error in errors if "consumer-graph limit" in error
                     or "distinct external consumer labels" in error],
                    [],
                    f"{label}: a config exactly on the ceiling must validate",
                )

    def test_one_entry_past_the_ceiling_is_still_refused_at_that_limit(self):
        """The premise: the patched ceiling really is the one being enforced.

        Without this the acceptance above would also hold if patching the
        constant had no effect on validation, which would make the control
        assert nothing at all.
        """
        for label, build in AT_LIMIT_CASES.items():
            with self.subTest(bound=label):
                config = self._config()
                build(config, PATCHED_LIMIT + 1)
                with patch.object(
                    config_module, "MAX_CONSUMER_GRAPH_ITEMS", PATCHED_LIMIT
                ):
                    errors = validate_config(config, self.scene.root)
                self.assertTrue(
                    [error for error in errors if "consumer-graph limit" in error
                     or "distinct external consumer labels" in error],
                    f"{label}: one past the ceiling must be refused, got {errors[:3]}",
                )

    def test_an_ordinary_invalid_config_produces_no_bound_diagnostic(self):
        """The control at the validation layer, where the CLI's is at the CLI.

        The diagnostics in `BOUND_CASES` have to be exclusive to their bounds
        for the command-level table to attribute anything, so a config that is
        invalid for an unrelated reason must produce none of them.
        """
        config = self._config()
        _an_unknown_consumer(config)
        errors = validate_config(config, self.scene.root)
        self.assertEqual(errors, [CONTROL_CASE[1]])
        for label, (_mutate, message) in BOUND_CASES.items():
            with self.subTest(bound=label):
                self.assertEqual(
                    [error for error in errors if error.startswith(message)], []
                )

    def test_the_repository_emits_a_closure_while_it_is_within_bounds(self):
        """The premise: all three commands do report the graph when allowed."""
        self.scene.file("api/a.txt", DRIFTED_CONTENT)

        generated = run_cli(self.scene.root, "generate")
        self.assertEqual(generated.returncode, 0, generated.stderr)
        self.assertIn(
            "Declared consumer edges (recorded in lock):", generated.stdout
        )
        self.assertIn("api -> components: web", generated.stdout)
        self.assertIn("api -> external consumers: team-x", generated.stdout)

        verified = run_cli(
            self.scene.root,
            "verify",
            "--source",
            "working-tree",
            "--format",
            "json",
        )
        self.assertEqual(verified.returncode, EXIT_BOUNDARY, verified.stderr)
        payload = json.loads(verified.stdout)
        self.assertEqual(
            payload["consumer_impact"],
            [
                {
                    "component": "api",
                    "components": ["web"],
                    "external_consumers": ["team-x"],
                    "facets": ["boundary"],
                    "transitive": False,
                }
            ],
        )
        self.assertIn("AFFECTED CONSUMERS api: team-x, web", payload["issues"])

        reviewed = run_cli(
            self.scene.root, "review", "HEAD~1..HEAD", "--format", "json"
        )
        self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
        impact = json.loads(reviewed.stdout)["consumer_impact"]
        self.assertEqual([row["component"] for row in impact], ["api"])
        self.assertEqual(
            impact[0]["components"], [{"name": "web", "source": "both"}]
        )

    def test_a_permitted_generate_rewrites_the_lock(self):
        """The premise for the unchanged-lock assertion in the table below.

        `generate` reads HEAD by default, so the closure premise above cannot
        stand in for this one: drifting a working-tree file and regenerating
        leaves boundary.lock.json byte-identical even though the command was
        allowed to run, which is asserted here first so the reason is visible.
        A committed config edit does move the file, and until something shows
        that, an unchanged lock after a refusal proves only that the lock is
        hard to move.
        """
        lock = self.scene.root / "boundary.lock.json"
        self.scene.file("api/a.txt", DRIFTED_CONTENT)
        drifted = run_cli(self.scene.root, "generate")
        self.assertEqual(drifted.returncode, 0, drifted.stderr)
        self.assertEqual(lock.read_bytes(), self.lock_bytes)
        self.scene.git("reset", "--hard")

        config = self._config()
        config["components"]["api"]["external_consumers"] = ["team-x", "team-y"]
        (self.scene.root / "boundary.config.json").write_text(
            json.dumps(config, indent=2) + "\n", encoding="utf-8"
        )
        self.scene.git("add", "--all")
        self.scene.git("commit", "-m", "one more external consumer")
        try:
            permitted = run_cli(self.scene.root, "generate")
            self.assertEqual(permitted.returncode, 0, permitted.stderr)
            self.assertIn(
                "api -> external consumers: team-x, team-y", permitted.stdout
            )
            self.assertNotEqual(
                lock.read_bytes(),
                self.lock_bytes,
                "a permitted generate left the lock alone, so the table's "
                "unchanged-lock assertion would hold either way",
            )
        finally:
            self.scene.git("reset", "--hard", "HEAD~1")
        self.assertEqual(lock.read_bytes(), self.lock_bytes)

    def test_a_tripped_bound_exits_two_and_lets_no_command_emit_a_closure(self):
        """Each row must show its own diagnostic, not merely be refused.

        The control row is the reason: an ordinary invalid config, with no
        bound anywhere near it, produces the same four refusals - exit 2 from
        all four commands, no closure marker, an unmoved lock, an empty
        `consumer_impact`. Asserting only `CONFIG INVALID` would therefore
        pass for every bound row with every bound check in _config.py deleted,
        because the published schema independently bounds `components`,
        `consumers`, `external_consumers` and the identifier length. The
        per-row diagnostic is the half of each row that only its own bound can
        produce, and the control asserts the absence of all of them.
        """
        for label, (mutate, diagnostic, is_bound) in REFUSAL_CASES.items():
            with self.subTest(case=label):
                try:
                    self._commit_invalid_config(mutate)

                    validated = run_cli(self.scene.root, "validate-config")
                    self.assertEqual(validated.returncode, EXIT_USAGE)
                    self.assertIn("CONFIG INVALID", validated.stdout)
                    self.assertIn(
                        diagnostic,
                        validated.stdout,
                        f"{label}: refused without its own diagnostic",
                    )
                    if not is_bound:
                        for other, (_m, message) in BOUND_CASES.items():
                            self.assertNotIn(
                                message,
                                validated.stdout,
                                f"the control produced the {other} diagnostic",
                            )

                    generated = run_cli(self.scene.root, "generate")
                    self.assertEqual(generated.returncode, EXIT_USAGE)
                    for marker in CLOSURE_MARKERS:
                        self.assertNotIn(marker, generated.stdout)
                    self.assertEqual(
                        (self.scene.root / "boundary.lock.json").read_bytes(),
                        self.lock_bytes,
                        "a refused generate rewrote the lock",
                    )

                    verified = run_cli(
                        self.scene.root,
                        "verify",
                        "--source",
                        "working-tree",
                        "--format",
                        "json",
                    )
                    self.assertEqual(verified.returncode, EXIT_USAGE)
                    payload = json.loads(verified.stdout)
                    self.assertEqual(payload["consumer_impact"], [])
                    self.assertEqual(
                        [
                            issue
                            for issue in payload["issues"]
                            if issue.startswith("AFFECTED CONSUMERS")
                        ],
                        [],
                    )

                    reviewed = run_cli(
                        self.scene.root, "review", "HEAD~1..HEAD", "--format", "json"
                    )
                    self.assertEqual(reviewed.returncode, EXIT_USAGE)
                    self.assertEqual(reviewed.stdout.strip(), "")
                    self.assertIn(
                        "review failed: target endpoint config is invalid",
                        reviewed.stderr,
                    )
                finally:
                    self.scene.git("reset", "--hard", "HEAD~1")


# --------------------------------------------------------------------------
# OBL-GIT-SOURCE-149
# --------------------------------------------------------------------------


class DiscoveryCeilingDenominatorTests(unittest.TestCase):
    """OBL-GIT-SOURCE-149: count the manifests discovery will actually use."""

    def test_git_declines_to_track_under_exactly_one_ignored_name(self):
        """The premise for every table below, and a check on the ignore set.

        `.git` is the only hardcoded ignore name that cannot hold a tracked
        manifest, so it is the only one the index-backed cases skip. If a
        future ignore name were also untrackable, or `.git` became trackable,
        this fails rather than letting a case quietly test nothing.
        """
        with Scenario() as scene:
            files = {
                f"{name}/pkg/package.json": PACKAGE_JSON
                for name in IGNORED_DIRECTORY_NAMES
            }
            _write_files(scene.root, files)
            _track_everything(scene.root)
            tracked = set(_tracked_paths(scene.root))
        untrackable = {
            name
            for name in IGNORED_DIRECTORY_NAMES
            if f"{name}/pkg/package.json" not in tracked
        }
        self.assertEqual(untrackable, {UNTRACKABLE_IGNORED_NAME})
        self.assertEqual(
            set(TRACKABLE_IGNORED_NAMES),
            set(IGNORED_DIRECTORY_NAMES) - untrackable,
        )

    def test_the_two_branches_are_different_code_paths(self):
        """The premise: a plain directory really does take the fallback."""
        files = _manifest_tree(["svc"], [])
        with Scenario() as scene:
            _write_files(scene.root, files)
            _track_everything(scene.root)
            git_outcome = _discover(scene.root, max_discovery_manifests=10)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_files(root, files)
            filesystem_outcome = _discover(root, max_discovery_manifests=10)
        self.assertNotIn(FALLBACK_WARNING, git_outcome.warnings)
        self.assertIn(FALLBACK_WARNING, filesystem_outcome.warnings)
        self.assertEqual(sorted(git_outcome.components), ["svc"])
        self.assertEqual(sorted(filesystem_outcome.components), ["svc"])

    def test_both_branches_refuse_when_the_visible_manifests_alone_exceed_it(self):
        """The premise: a refusal is observable through either branch."""
        files = _manifest_tree(["svc", "web", "tools"], [])
        with Scenario() as scene:
            _write_files(scene.root, files)
            _track_everything(scene.root)
            git_outcome = _discover(scene.root, max_discovery_manifests=2)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_files(root, files)
            filesystem_outcome = _discover(root, max_discovery_manifests=2)
        expected = "Component discovery guardrail exceeded: >2 manifests"
        self.assertEqual(git_outcome.refusal, expected)
        self.assertEqual(filesystem_outcome.refusal, expected)

    def test_both_branches_skip_ignored_manifests_when_the_ceiling_is_generous(
        self,
    ):
        """The clause that holds: the skip itself is identical either way."""
        files = _manifest_tree(["svc"], list(TRACKABLE_IGNORED_NAMES))
        generous = len(files) + 1
        with Scenario() as scene:
            _write_files(scene.root, files)
            _track_everything(scene.root)
            git_outcome = _discover(scene.root, max_discovery_manifests=generous)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_files(root, files)
            filesystem_outcome = _discover(
                root, max_discovery_manifests=generous
            )
        self.assertEqual(sorted(git_outcome.components), ["svc"])
        self.assertEqual(
            sorted(git_outcome.components), sorted(filesystem_outcome.components)
        )

    def test_an_ignored_prefix_leaves_the_ceiling_denominator_in_both_branches(
        self,
    ):
        """Built-in and explicit exclusions are both applied before counting."""
        files = _manifest_tree(["svc", "web"], ["vendor"])
        with Scenario() as scene:
            _write_files(scene.root, files)
            _track_everything(scene.root)
            without_exclusion = _discover(
                scene.root, max_discovery_manifests=2
            )
            with_exclusion = _discover(
                scene.root, excluded_paths=["vendor"], max_discovery_manifests=2
            )
        self.assertIsNone(without_exclusion.refusal)
        self.assertEqual(sorted(without_exclusion.components), ["svc", "web"])
        self.assertIsNone(with_exclusion.refusal)
        self.assertEqual(sorted(with_exclusion.components), ["svc", "web"])

    def test_the_two_branches_agree_about_every_ignored_name(self):
        """The Git-index and filesystem paths use the same ceiling denominator."""
        for name in TRACKABLE_IGNORED_NAMES:
            with self.subTest(ignored=name):
                files = _manifest_tree(["svc"], [name])
                with Scenario() as scene:
                    _write_files(scene.root, files)
                    _track_everything(scene.root)
                    git_outcome = _discover(scene.root, max_discovery_manifests=1)
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    _write_files(root, files)
                    filesystem_outcome = _discover(
                        root, max_discovery_manifests=1
                    )
                self.assertIsNone(git_outcome.refusal)
                self.assertIsNone(filesystem_outcome.refusal)
                self.assertEqual(sorted(git_outcome.components), ["svc"])
                self.assertEqual(git_outcome.components, filesystem_outcome.components)

    @DISCOVERY_PROFILE
    @given(
        visible=st.lists(
            st.sampled_from(GENERATED_COMPONENT_DIRECTORIES),
            min_size=1,
            max_size=len(GENERATED_COMPONENT_DIRECTORIES),
            unique=True,
        ),
        hidden=st.lists(
            st.sampled_from(TRACKABLE_IGNORED_NAMES),
            max_size=3,
            unique=True,
        ),
    )
    def test_the_filesystem_ceiling_counts_only_what_discovery_considers(
        self, visible, hidden
    ):
        """The oracle is the generated tree, not the function under test."""
        files = _manifest_tree(visible, hidden)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_files(root, files)
            outcome = _discover(root, max_discovery_manifests=len(visible))
        self.assertIsNone(outcome.refusal)
        self.assertEqual(sorted(outcome.components), sorted(visible))

    @DISCOVERY_PROFILE
    @given(
        visible=st.lists(
            st.sampled_from(GENERATED_COMPONENT_DIRECTORIES),
            min_size=1,
            max_size=len(GENERATED_COMPONENT_DIRECTORIES),
            unique=True,
        ),
        hidden=st.lists(
            st.sampled_from(TRACKABLE_IGNORED_NAMES),
            min_size=1,
            max_size=3,
            unique=True,
        ),
    )
    def test_the_index_backed_ceiling_counts_only_what_discovery_considers(
        self, visible, hidden
    ):
        """Tracked manifests in ignored trees do not consume the ceiling."""
        files = _manifest_tree(visible, hidden)
        with Scenario() as scene:
            _write_files(scene.root, files)
            _track_everything(scene.root)
            outcome = _discover(scene.root, max_discovery_manifests=len(visible))
        self.assertIsNone(outcome.refusal)
        self.assertEqual(sorted(outcome.components), sorted(visible))


# --------------------------------------------------------------------------
# OBL-GIT-SOURCE-150
# --------------------------------------------------------------------------


class DiscoveredConfigValidityTests(unittest.TestCase):
    """OBL-GIT-SOURCE-150: whatever discovery emits must validate."""

    def test_every_declared_manifest_kind_has_a_fixture_body(self):
        """A manifest added to `manifest_specs` must be generated here too."""
        self.assertEqual(set(MANIFEST_NAMES), set(MANIFEST_BODIES))

    def test_no_generated_component_directory_is_a_conventional_root(self):
        """What the generated shapes assume about the derived root list.

        `_repository_shapes` draws its component directories from one tuple
        and its planted conventional root from the other, and reads the result
        as a repository where the remap fires only because the shape asked for
        it. A root added to `discover_components`' loop that collides with a
        generated directory name would make that reading false everywhere it
        is used, silently, so the derivation is checked against the assumption
        rather than trusted.
        """
        self.assertTrue(CONVENTIONAL_ROOTS)
        self.assertEqual(
            sorted(set(CONVENTIONAL_ROOTS) & set(GENERATED_COMPONENT_DIRECTORIES)),
            [],
        )

    def test_a_healthy_repository_discovers_a_config_that_validates(self):
        """The premise: this pipeline does return an empty error list."""
        with Scenario() as scene:
            _write_files(scene.root, {"svc/package.json": PACKAGE_JSON})
            _track_everything(scene.root)
            outcome = _discover(scene.root)
            self.assertIsNone(outcome.refusal)
            self.assertEqual(sorted(outcome.components), ["svc"])
            errors = validate_config(
                _init_style_config(scene.root, outcome.components), scene.root
            )
        self.assertEqual(errors, [])

    def test_init_discover_then_validate_config_succeeds_on_a_healthy_repository(
        self,
    ):
        """The premise for the command-level pinning below."""
        with Scenario() as scene:
            _write_files(scene.root, {"svc/package.json": PACKAGE_JSON})
            _track_everything(scene.root)
            created = run_cli(scene.root, "init", "--discover")
            self.assertEqual(created.returncode, 0, created.stderr)
            validated = run_cli(scene.root, "validate-config")
        self.assertEqual(validated.returncode, 0, validated.stdout)
        self.assertIn("Config is valid.", validated.stdout)

    def test_a_tracked_file_named_like_a_conventional_root_is_not_remapped_onto(
        self,
    ):
        """A conventional root requires a tracked descendant, not its name."""
        for conventional in CONVENTIONAL_ROOTS:
            with self.subTest(conventional=conventional):
                with Scenario() as scene:
                    _write_files(
                        scene.root,
                        {
                            "package.json": PACKAGE_JSON,
                            conventional: "a regular file\n",
                        },
                    )
                    _track_everything(scene.root)
                    outcome = _discover(scene.root)
                    if outcome.refused or not outcome.components:
                        continue
                    errors = validate_config(
                        _init_style_config(scene.root, outcome.components),
                        scene.root,
                    )
                self.assertEqual(errors, [])

    def test_a_tracked_file_named_like_a_conventional_root_is_not_a_component(
        self,
    ):
        """The unusable root manifest is skipped instead of emitting bad config."""
        for conventional in CONVENTIONAL_ROOTS:
            with self.subTest(conventional=conventional):
                with Scenario() as scene:
                    _write_files(
                        scene.root,
                        {
                            "package.json": PACKAGE_JSON,
                            conventional: "a regular file\n",
                        },
                    )
                    _track_everything(scene.root)
                    outcome = _discover(scene.root)
                    self.assertIsNone(outcome.refusal)
                    self.assertEqual(outcome.components, {})

    def test_an_index_only_manifest_does_not_become_a_component(self):
        """An unstaged manifest deletion refuses rather than erasing a component."""
        with Scenario() as scene:
            _write_files(scene.root, {"svc/package.json": PACKAGE_JSON})
            _track_everything(scene.root)
            shutil.rmtree(scene.root / "svc")
            outcome = _discover(scene.root)
            if outcome.refused:
                return
            errors = validate_config(
                _init_style_config(scene.root, outcome.components), scene.root
            )
        self.assertEqual(errors, [])

    def test_an_index_only_manifest_refusal_is_actionable(
        self,
    ):
        """The refusal identifies the missing index entry and recovery choices."""
        with Scenario() as scene:
            _write_files(scene.root, {"svc/package.json": PACKAGE_JSON})
            _track_everything(scene.root)
            shutil.rmtree(scene.root / "svc")
            outcome = _discover(scene.root)
            self.assertIsNone(outcome.components)
            self.assertIn("svc/package.json", outcome.refusal)
            self.assertIn("Restore or stage its deletion", outcome.refusal)

    def test_init_discover_does_not_write_an_invalid_config(
        self,
    ):
        """Both problematic repository shapes refuse before writing config."""
        shapes = {
            "tracked file named src": (
                {"package.json": PACKAGE_JSON, "src": "a regular file\n"},
                None,
            ),
            "index-only manifest": (
                {"svc/package.json": PACKAGE_JSON},
                "svc",
            ),
        }
        for label, (files, removed) in shapes.items():
            with self.subTest(shape=label):
                with Scenario() as scene:
                    _write_files(scene.root, files)
                    _track_everything(scene.root)
                    if removed is not None:
                        shutil.rmtree(scene.root / removed)
                    created = run_cli(scene.root, "init", "--discover")
                    self.assertEqual(created.returncode, EXIT_USAGE)
                    self.assertFalse(
                        (scene.root / "boundary.config.json").exists()
                    )
                    self.assertTrue(created.stderr or created.stdout)

    @DISCOVERY_PROFILE
    @given(shape=_repository_shapes(defective=False))
    def test_every_healthy_repository_shape_discovers_a_valid_config(self, shape):
        """The premise: over generated repositories this really can hold."""
        with Scenario() as scene:
            _build_shape(scene, shape)
            outcome = _discover(scene.root)
            if outcome.refused:
                return
            if not outcome.components:
                # `_cmd_init` refuses an empty discovery before writing, so
                # there is no config for validate_config to judge.
                return
            errors = validate_config(
                _init_style_config(scene.root, outcome.components), scene.root
            )
        self.assertEqual(errors, [], f"shape {shape} emitted {outcome.components}")

    @DISCOVERY_PROFILE
    @given(shape=_repository_shapes(defective=True))
    def test_every_repository_shape_discovers_a_valid_config_or_refuses(
        self, shape
    ):
        """Every generated repository either yields valid config or refuses."""
        with Scenario() as scene:
            _build_shape(scene, shape)
            outcome = _discover(scene.root)
            if outcome.refused:
                return
            if not outcome.components:
                return
            errors = validate_config(
                _init_style_config(scene.root, outcome.components), scene.root
            )
        self.assertEqual(errors, [], f"shape {shape} emitted {outcome.components}")


if __name__ == "__main__":
    unittest.main()
