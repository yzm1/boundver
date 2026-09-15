"""Custom-provider authorization, refusal, and Git-history tests.

A repository may declare Python providers, but only an explicit caller option
may import them. Once authorized, malformed or hostile providers must produce
bounded load errors without mutating either provider registry or terminating a
verification process.

Covers OBL-PROVIDERS-020, OBL-PROVIDERS-037, OBL-PROVIDERS-038,
OBL-PROVIDERS-041, OBL-PROVIDERS-047 and OBL-GIT-SOURCE-127.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import os
import sys
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List, Tuple

import boundver
from boundver import core, providers
from boundver._cli_parser import build_parser
from boundver._config import validate_config
from boundver._git import _capture_git_source_snapshot
from boundver._lockfile import generate_lockfile
from boundver._output import (
    _component_lock_history_base,
    _resolve_lock_history_base,
    analyze_component_drift,
)
from boundver._structural_review import _resolved_registries
from boundver._utils import ProviderError
from boundver.providers import (
    ResolvedBoundary,
    create_registry,
    load_custom_providers,
)

from tests._parity import run_cli
from tests._scenarios import Scenario

LOCK = "boundary.lock.json"

# ---------------------------------------------------------------------------
# The provider classes the loader tests declare. One module holds all of them,
# injected into sys.modules the way tests/test_custom_provider_names.py does,
# because these tests are about the loader's bookkeeping and not about import.
# ---------------------------------------------------------------------------

FIXTURE_MODULE = "_boundver_chunk01_fixture"
TRAP_MODULE = FIXTURE_MODULE + "_trap"
EXITING_TRAP_MODULE = FIXTURE_MODULE + "_exiting_trap"
ABSENT_MODULE = FIXTURE_MODULE + "_absent"


class Good:
    """A well-formed custom provider that resolves one declared file."""

    name = "custom.good"
    version = "1"

    def resolve(self, ctx):
        entries = []
        for repo_rel in sorted(ctx.list_files(ctx.component_path + "/api.json")):
            entries.append(("file:api.json", ctx.read_file(repo_rel)))
        return ResolvedBoundary(entries=entries)


class Second:
    """A second well-formed provider, for asserting a two-entry load."""

    name = "custom.second"
    version = "1"

    def resolve(self, ctx):
        return ResolvedBoundary(entries=[("contract", b"second")])


class BadConstructor:
    def __init__(self):
        raise RuntimeError("constructor blew up")


class UnreadableName:
    version = "1"

    @property
    def name(self):
        raise RuntimeError("name is a trap")

    def resolve(self, ctx):
        return ResolvedBoundary(entries=[])


class NonStringName:
    name = 123
    version = "1"

    def resolve(self, ctx):
        return ResolvedBoundary(entries=[])


class BuiltinLookingName:
    name = "path-hash"
    version = "1"

    def resolve(self, ctx):
        return ResolvedBoundary(entries=[])


class BareCustomName:
    name = "custom."
    version = "1"

    def resolve(self, ctx):
        return ResolvedBoundary(entries=[])


class BadVersion:
    name = "custom.badversion"
    version = ""

    def resolve(self, ctx):
        return ResolvedBoundary(entries=[])


class ExitingName:
    version = "1"

    @property
    def name(self):
        raise SystemExit(3)

    def resolve(self, ctx):
        return ResolvedBoundary(entries=[])


class InterruptedConstructor:
    def __init__(self):
        raise KeyboardInterrupt()


class FlakySecond:
    """Constructs exactly once, so two loads of one config disagree."""

    constructions = 0
    name = "custom.flaky"
    version = "1"

    def __init__(self):
        type(self).constructions += 1
        if type(self).constructions > 1:
            raise RuntimeError("this provider constructs exactly once")

    def resolve(self, ctx):
        return ResolvedBoundary(entries=[])


class _TrapModule(types.ModuleType):
    """A module whose every attribute read raises, to reach the getattr guard."""

    def __getattr__(self, attribute):
        raise ValueError(f"attribute {attribute} is a trap")


class _ExitingTrapModule(types.ModuleType):
    """The same guard, reached with a BaseException instead of an Exception."""

    def __getattr__(self, attribute):
        raise SystemExit(7)


_FIXTURE_CLASSES = (
    Good,
    Second,
    BadConstructor,
    UnreadableName,
    NonStringName,
    BuiltinLookingName,
    BareCustomName,
    BadVersion,
    ExitingName,
    InterruptedConstructor,
    FlakySecond,
)


def install_fixture_modules() -> None:
    module = types.ModuleType(FIXTURE_MODULE)
    for cls in _FIXTURE_CLASSES:
        setattr(module, cls.__name__, cls)
    sys.modules[FIXTURE_MODULE] = module
    sys.modules[TRAP_MODULE] = _TrapModule(TRAP_MODULE)
    sys.modules[EXITING_TRAP_MODULE] = _ExitingTrapModule(EXITING_TRAP_MODULE)


def remove_fixture_modules() -> None:
    for name in (
        FIXTURE_MODULE,
        TRAP_MODULE,
        EXITING_TRAP_MODULE,
        ABSENT_MODULE,
    ):
        sys.modules.pop(name, None)


def declaration(class_name: str, **extra: Any) -> dict:
    entry = {"module": FIXTURE_MODULE, "class": class_name}
    entry.update(extra)
    return entry


class _RegistryIsolation(unittest.TestCase):
    """Restore the process-global registry and the stateful fixture classes."""

    def setUp(self):
        install_fixture_modules()
        FlakySecond.constructions = 0
        self._global_registry = dict(providers._REGISTRY)

    def tearDown(self):
        providers._REGISTRY.clear()
        providers._REGISTRY.update(self._global_registry)
        FlakySecond.constructions = 0
        remove_fixture_modules()


# ---------------------------------------------------------------------------
# OBL-PROVIDERS-047: which of the four opening gates answers first
# ---------------------------------------------------------------------------

TRUST_ERROR = (
    "Config declares custom providers but loading is not enabled. "
    "Pass --allow-custom-providers (or the equivalent trusted API argument)."
)
#: The first sentence of the refusal, which is what the CLI embeds in the
#: several different envelopes its commands wrap it in.
REFUSAL_SENTENCE = "Config declares custom providers but loading is not enabled"
SHAPE_ERROR = "Config providers must be an array"
COUNT_ERROR = "Config providers exceed the 100-provider limit"


class _ListSubclass(list):
    """A list by isinstance and not by `type(...) is list`."""


#: Falsy values that are not lists. The emptiness check at :1526 runs before
#: the trust gate at :1528, so each of these returns an empty error list even
#: with allow_custom False - which is to say the trust gate is not reached at
#: all, and the existing trust obligation is false for exactly these values.
FALSY_NON_LISTS = {
    "empty list": [],
    "empty dict": {},
    "empty string": "",
    "zero": 0,
    "false": False,
    "none": None,
}

#: Non-empty values that are not `list` exactly. The trust gate at :1528 runs
#: before the shape check at :1533, so the answer depends on allow_custom: the
#: trust error when it is False, the shape error when it is True. A tuple and a
#: list subclass are here because the check is `type(...) is not list`.
NON_EMPTY_NON_LISTS = {
    "populated dict": {"a": 1},
    "text": "providers",
    "integer": 7,
    "tuple": ({"module": FIXTURE_MODULE, "class": "Good"},),
    "list subclass": _ListSubclass([{"module": FIXTURE_MODULE, "class": "Good"}]),
}


class LoaderGateOrderTests(_RegistryIsolation):
    """OBL-PROVIDERS-047: emptiness, then trust, then shape, then count."""

    def test_a_falsy_non_list_returns_no_error_even_when_loading_is_forbidden(self):
        for label, value in sorted(FALSY_NON_LISTS.items()):
            for allow in (False, True):
                with self.subTest(value=label, allow_custom=allow):
                    registry = create_registry()
                    self.assertEqual(
                        load_custom_providers(value, allow, registry=registry),
                        [],
                    )
                    self.assertEqual(sorted(registry), sorted(create_registry()))

    def test_a_non_empty_non_list_gets_the_trust_error_before_the_shape_error(self):
        for label, value in sorted(NON_EMPTY_NON_LISTS.items()):
            with self.subTest(value=label):
                self.assertEqual(
                    load_custom_providers(value, False, registry=create_registry()),
                    [TRUST_ERROR],
                )

    def test_a_non_empty_non_list_gets_the_shape_error_once_loading_is_allowed(self):
        """The premise for the test above: the shape error does exist, and this

        is the only spelling of the call that reaches it. A tuple and a list
        subclass are rejected here because `type(providers_list) is not list`
        is not `isinstance`.
        """
        for label, value in sorted(NON_EMPTY_NON_LISTS.items()):
            with self.subTest(value=label):
                self.assertEqual(
                    load_custom_providers(value, True, registry=create_registry()),
                    [SHAPE_ERROR],
                )

    def test_the_count_ceiling_is_checked_after_the_shape_of_the_container(self):
        oversized = [declaration("Good")] * (providers.MAX_CUSTOM_PROVIDERS + 1)
        self.assertEqual(
            load_custom_providers(oversized, True, registry=create_registry()),
            [COUNT_ERROR],
        )
        self.assertEqual(
            load_custom_providers(tuple(oversized), True, registry=create_registry()),
            [SHAPE_ERROR],
        )

    def test_the_gates_appear_in_the_source_in_the_order_they_are_asserted(self):
        """Read the four gates out of the function, so a reorder is a failure."""
        source = inspect.cleandoc(inspect.getsource(providers.load_custom_providers))
        tree = ast.parse(source)
        function = tree.body[0]
        loop = next(node for node in function.body if isinstance(node, ast.For))
        texts = []
        for node in ast.walk(function):
            if isinstance(node, ast.Return) and node.lineno < loop.lineno:
                texts.append((node.lineno, ast.dump(node)))
        ordered = [dump for _, dump in sorted(texts)]
        self.assertEqual(len(ordered), 4, ordered)
        self.assertEqual(ordered[0], "Return(value=List(ctx=Load()))")
        self.assertIn("loading is not enabled", ordered[1])
        self.assertIn(SHAPE_ERROR, ordered[2])
        self.assertIn("provider limit", ordered[3])


# ---------------------------------------------------------------------------
# OBL-PROVIDERS-037: a refused declaration leaves nothing behind
# ---------------------------------------------------------------------------

#: One row per rejection spelling: the entries loaded in a first call, the
#: entries handed to the call under test, the single error that call must
#: return, and the names that call is nevertheless allowed to register. Only
#: "duplicate within one call" has a non-empty last field, because that branch
#: is only reachable when a valid entry precedes the rejected one in the SAME
#: call; every other row must add nothing at all.
#:
#: Two pairs of rows share a branch on purpose - a missing `module` and a
#: non-string one both land at :1554, and a built-in-looking name and a bare
#: "custom." both land in the same guard, so there are seventeen rows over
#: fifteen
#: branches, and `test_the_table_covers_every_rejection_branch` pins that
#: arithmetic against the function's syntax tree.
REJECTIONS: Dict[str, Tuple[List[dict], List[Any], str, set]] = {
    "entry is not an object": (
        [],
        ["not a dict"],
        "Provider entry must be an object, got str",
        set(),
    ),
    "module field missing": (
        [],
        [{}],
        "Provider entry missing required fields 'module'/'class': {}",
        set(),
    ),
    "module field is not text": (
        [],
        [{"module": 5, "class": "Good"}],
        "Provider entry missing required fields 'module'/'class': "
        "{'module': 5, 'class': 'Good'}",
        set(),
    ),
    "module name is not an identifier path": (
        [],
        [{"module": "not-a-module!", "class": "Good"}],
        "Provider module name 'not-a-module!' is not a valid Python module path "
        "(must be dotted identifiers like 'my_pkg.providers')",
        set(),
    ),
    "class name is not an identifier": (
        [],
        [declaration("not an ident")],
        "Provider class name 'not an ident' is not a valid Python identifier",
        set(),
    ),
    "module import fails": (
        [],
        [{"module": ABSENT_MODULE, "class": "Good"}],
        f"Failed to import provider module '{ABSENT_MODULE}': "
        f"No module named '{ABSENT_MODULE}'",
        set(),
    ),
    "class attribute is unreadable": (
        [],
        [{"module": TRAP_MODULE, "class": "Good"}],
        f"Failed to read '{TRAP_MODULE}.Good': attribute Good is a trap",
        set(),
    ),
    "class is absent": (
        [],
        [declaration("Missing")],
        f"Module '{FIXTURE_MODULE}' has no attribute 'Missing'",
        set(),
    ),
    "constructor raises": (
        [],
        [declaration("BadConstructor")],
        f"Failed to instantiate '{FIXTURE_MODULE}.BadConstructor': "
        "constructor blew up",
        set(),
    ),
    "name attribute is unreadable": (
        [],
        [declaration("UnreadableName")],
        f"Provider '{FIXTURE_MODULE}.UnreadableName' is invalid: "
        "Provider attribute 'name' could not be read: name is a trap",
        set(),
    ),
    "name is not text": (
        [],
        [declaration("NonStringName")],
        f"Provider '{FIXTURE_MODULE}.NonStringName' changed its name "
        "during validation",
        set(),
    ),
    "name is a built-in name": (
        [],
        [declaration("BuiltinLookingName")],
        f"Provider '{FIXTURE_MODULE}.BuiltinLookingName' has name='path-hash'; "
        "custom provider names must start with 'custom.' to avoid collisions "
        "with built-in providers (e.g. name='custom.my_format')",
        set(),
    ),
    "name is exactly custom.": (
        [],
        [declaration("BareCustomName")],
        f"Provider '{FIXTURE_MODULE}.BareCustomName' has name='custom.'; "
        "custom provider names must start with 'custom.' to avoid collisions "
        "with built-in providers (e.g. name='custom.my_format')",
        set(),
    ),
    "configured name disagrees": (
        [],
        [declaration("Good", name="custom.other")],
        f"Provider '{FIXTURE_MODULE}.Good' declares runtime name='custom.good', "
        "which does not match configured name='custom.other'",
        set(),
    ),
    "duplicate within one call": (
        [],
        [declaration("Good"), declaration("Good")],
        "Duplicate custom provider name 'custom.good' in providers config",
        {"custom.good"},
    ),
    "collides with the registry": (
        [declaration("Good")],
        [declaration("Good")],
        "Custom provider name 'custom.good' is already registered; "
        "refusing to replace it while loading config",
        set(),
    ),
    "identity contract violated": (
        [],
        [declaration("BadVersion")],
        f"Provider '{FIXTURE_MODULE}.BadVersion' is invalid: "
        "Provider version must be a non-empty string",
        set(),
    ),
}


def _snapshot(registry: dict) -> Dict[str, int]:
    """Key set and stored-instance identity, which is what must not move."""
    return {key: id(value) for key, value in registry.items()}


class RejectedDeclarationLeavesNoResidueTests(_RegistryIsolation):
    """OBL-PROVIDERS-037: fifteen refusal branches leave no partial registration."""

    def test_a_declaration_that_is_accepted_does_change_the_registry(self):
        """The premise. Every assertion below says a key set did not move; this

        one says the key set moves when a declaration is accepted, so that the
        others are measuring a live mechanism rather than an inert one.
        """
        registry = create_registry()
        before = _snapshot(registry)
        self.assertEqual(
            load_custom_providers(
                [declaration("Good"), declaration("Second")],
                True,
                registry=registry,
            ),
            [],
        )
        self.assertEqual(
            set(registry) - set(before), {"custom.good", "custom.second"}
        )
        self.assertIsInstance(registry["custom.good"], Good)

    def test_a_declaration_that_is_accepted_does_change_the_global_registry(self):
        """The premise for the `registry=None` half: the global is writable."""
        before = _snapshot(providers._REGISTRY)
        self.assertEqual(
            load_custom_providers([declaration("Good")], True, registry=None), []
        )
        self.assertEqual(set(providers._REGISTRY) - set(before), {"custom.good"})

    def test_each_rejection_leaves_the_target_registry_exactly_as_it_was(self):
        for label, (preload, entries, _error, added) in sorted(REJECTIONS.items()):
            with self.subTest(rejection=label):
                registry = create_registry()
                if preload:
                    self.assertEqual(
                        load_custom_providers(preload, True, registry=registry), []
                    )
                before = _snapshot(registry)
                load_custom_providers(entries, True, registry=registry)
                self.assertEqual(set(registry) - set(before), added)
                for key, identity in before.items():
                    self.assertIn(key, registry, key)
                    self.assertEqual(id(registry[key]), identity, key)

    def test_each_rejection_returns_exactly_the_one_error_it_is_named_for(self):
        for label, (preload, entries, error, _added) in sorted(REJECTIONS.items()):
            with self.subTest(rejection=label):
                registry = create_registry()
                if preload:
                    load_custom_providers(preload, True, registry=registry)
                self.assertEqual(
                    load_custom_providers(entries, True, registry=registry), [error]
                )

    def test_each_rejection_leaves_the_global_registry_alone_when_given_a_registry(self):
        for label, (preload, entries, _error, _added) in sorted(REJECTIONS.items()):
            with self.subTest(rejection=label):
                registry = create_registry()
                before = _snapshot(providers._REGISTRY)
                if preload:
                    load_custom_providers(preload, True, registry=registry)
                load_custom_providers(entries, True, registry=registry)
                self.assertEqual(_snapshot(providers._REGISTRY), before)

    def test_each_rejection_leaves_the_global_registry_as_it_was_without_a_registry(self):
        """The `registry=None` form writes to `_REGISTRY`, so measure it there."""
        for label, (preload, entries, _error, added) in sorted(REJECTIONS.items()):
            with self.subTest(rejection=label):
                providers._REGISTRY.clear()
                providers._REGISTRY.update(self._global_registry)
                if preload:
                    self.assertEqual(
                        load_custom_providers(preload, True, registry=None), []
                    )
                before = _snapshot(providers._REGISTRY)
                load_custom_providers(entries, True, registry=None)
                self.assertEqual(set(providers._REGISTRY) - set(before), added)
                for key, identity in before.items():
                    self.assertIn(key, providers._REGISTRY, key)
                    self.assertEqual(id(providers._REGISTRY[key]), identity, key)

    def test_every_rejection_the_loader_returned_worded_itself_differently(self):
        """Seventeen rows, seventeen messages, collected from the loader itself.

        This is table hygiene rather than branch coverage, and the distinction
        matters: distinct messages do NOT imply distinct branches, as two rows
        here demonstrate. "module field missing" and "module field is not text"
        say different things because the message interpolates the offending
        entry, but both land on the single `errors.append` at providers.py:1554.
        What this does buy is that no two rows can quietly collapse into copies
        of one another, which would leave a named rejection untested while the
        row count still looked right. The evidence for branch coverage is
        `test_the_table_covers_every_rejection_branch_in_the_per_entry_loop`,
        which counts the branches in the source.

        The messages are read out of what `load_custom_providers` actually
        returned rather than out of the table above, so this is an assertion
        about the code. A table-only version would pass for a file that never
        called boundver at all.
        """
        observed: Dict[str, str] = {}
        for label, (preload, entries, _error, _added) in sorted(REJECTIONS.items()):
            with self.subTest(rejection=label):
                registry = create_registry()
                if preload:
                    load_custom_providers(preload, True, registry=registry)
                errors = load_custom_providers(entries, True, registry=registry)
                self.assertEqual(len(errors), 1, errors)
                observed[label] = errors[0]
        self.assertEqual(len(observed), len(REJECTIONS))
        self.assertEqual(len(set(observed.values())), len(REJECTIONS), observed)

    def test_the_table_covers_every_rejection_branch_in_the_per_entry_loop(self):
        """Count the branches in the source, not in this file's imagination.

        Every rejection ends in `errors.append(...)` followed by `continue`
        inside the per-entry loop, so counting those calls counts the branches.
        Seventeen rows cover fifteen branches because two pairs of rows share
        one, which is written into the row names and re-checked here.
        """
        source = inspect.cleandoc(inspect.getsource(providers.load_custom_providers))
        function = ast.parse(source).body[0]
        loop = next(node for node in function.body if isinstance(node, ast.For))
        appends = [
            node
            for node in ast.walk(loop)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "errors"
        ]
        shared = {
            ("module field missing", "module field is not text"),
            ("name is a built-in name", "name is exactly custom."),
        }
        self.assertEqual(len(appends), len(REJECTIONS) - len(shared))
        for pair in shared:
            for label in pair:
                self.assertIn(label, REJECTIONS)

    def test_registry_writers_are_explicit_and_enumerated(self):
        """The residue claim rests on knowing who can write; enumerate them.

        A dict can be written two ways, and counting only one of them is how an
        enumeration like this goes quietly wrong. `reg[key] = value` is an
        `ast.Subscript` store, but `reg.update(...)` is an ordinary method call
        and looks nothing like it in the tree - so a sweep for subscripts alone
        would miss `target_registry.update({name: instance})` smuggled into the
        loader. It also misses a writer that is really there: `_register_builtins`
        populates the process-global with `_REGISTRY.update(create_registry())`
        and has no subscript in it at all. Both spellings are collected here,
        which is why this test names three writers where a subscript-only sweep
        names two.

        `register_provider` stores a directly registered provider,
        `create_registry` stores built-in aliases, `load_custom_providers`
        stores a provider under the single name it validated, and
        `_register_builtins` seeds the process-global registry once at import.
        """
        module = ast.parse(inspect.cleandoc(inspect.getsource(providers)))
        subscript_writers: Dict[str, List[str]] = {}
        method_writers: Dict[str, List[str]] = {}
        for node in ast.walk(module):
            if not isinstance(node, ast.FunctionDef):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Subscript) and isinstance(
                    inner.ctx, ast.Store
                ):
                    subscript_writers.setdefault(node.name, []).append(
                        ast.dump(inner)
                    )
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr
                    in {"update", "setdefault", "__setitem__", "pop", "clear"}
                ):
                    method_writers.setdefault(node.name, []).append(inner.func.attr)
        self.assertEqual(
            sorted(subscript_writers),
            ["create_registry", "load_custom_providers", "register_provider"],
            subscript_writers,
        )
        self.assertEqual(
            sorted(method_writers), ["_register_builtins"], method_writers
        )
        self.assertEqual(method_writers["_register_builtins"], ["update"])
        self.assertEqual(len(subscript_writers["register_provider"]), 1)
        self.assertEqual(len(subscript_writers["create_registry"]), 1)
        self.assertEqual(len(subscript_writers["load_custom_providers"]), 1)
        self.assertIn("id='alias'", subscript_writers["create_registry"][0])
        self.assertIn(
            "id='provider_name'", subscript_writers["load_custom_providers"][0]
        )
        self.assertNotIn("load_custom_providers", method_writers)


# ---------------------------------------------------------------------------
# OBL-PROVIDERS-038: per-entry loading, and what four callers do about it
# ---------------------------------------------------------------------------


def _custom_provider_scenario(providers_list: List[dict]) -> Scenario:
    scene = Scenario("chunk01")
    scene.component("svc", path="svc", provider="custom.good", boundary=["api.json"])
    scene.config["providers"] = list(providers_list)
    scene.file("svc/api.json", '{"v": 1}\n')
    scene.commit("declare a custom provider")
    return scene


class PartialLoadSemanticsTests(_RegistryIsolation):
    """OBL-PROVIDERS-038: the loader is per-entry, so callers must be atomic."""

    def test_a_broken_entry_after_a_valid_one_leaves_the_valid_one_registered(self):
        registry = create_registry()
        builtins = set(registry)
        errors = load_custom_providers(
            [declaration("Good"), declaration("Missing")], True, registry=registry
        )
        self.assertEqual(errors, [f"Module '{FIXTURE_MODULE}' has no attribute 'Missing'"])
        self.assertEqual(set(registry) - builtins, {"custom.good"})

    def test_a_broken_entry_before_a_valid_one_leaves_the_valid_one_registered(self):
        registry = create_registry()
        builtins = set(registry)
        errors = load_custom_providers(
            [declaration("Missing"), declaration("Good")], True, registry=registry
        )
        self.assertEqual(errors, [f"Module '{FIXTURE_MODULE}' has no attribute 'Missing'"])
        self.assertEqual(set(registry) - builtins, {"custom.good"})

    def test_generate_lockfile_produces_a_lock_when_every_entry_loads(self):
        """The premise for the next test: this configuration does generate."""
        with _custom_provider_scenario([declaration("Good")]) as scene:
            lockfile = generate_lockfile(
                scene.config, scene.root, source="head", allow_custom_providers=True
            )
            self.assertIsNotNone(
                lockfile["components"]["svc"]["fingerprints"]["boundary"]
            )

    def test_generate_lockfile_abandons_the_operation_on_any_load_error(self):
        with _custom_provider_scenario(
            [declaration("Good"), declaration("Missing")]
        ) as scene:
            with self.assertRaises(ProviderError) as raised:
                generate_lockfile(
                    scene.config, scene.root, source="head", allow_custom_providers=True
                )
        self.assertEqual(
            str(raised.exception),
            "Custom provider loading failed:\n"
            f"Module '{FIXTURE_MODULE}' has no attribute 'Missing'",
        )

    def test_validate_config_surfaces_load_errors_only_when_loading_is_allowed(self):
        with _custom_provider_scenario(
            [declaration("Good"), declaration("Missing")]
        ) as scene:
            allowed = validate_config(
                scene.config, scene.root, source="head", allow_custom_providers=True
            )
            refused = validate_config(
                scene.config, scene.root, source="head", allow_custom_providers=False
            )
        # The premise and the claim in one place: the error exists, and the
        # refusing call drops it rather than never producing it.
        self.assertEqual(
            allowed, [f"Module '{FIXTURE_MODULE}' has no attribute 'Missing'"]
        )
        self.assertEqual(refused, [])

    def test_structural_review_builds_registries_without_loading_when_refused(self):
        config = {"providers": [declaration("Good")]}
        base, target, detail = _resolved_registries(
            config, config, allow_custom_providers=False
        )
        self.assertIsNone(detail)
        for label, registry in (("base", base), ("target", target)):
            with self.subTest(registry=label):
                self.assertEqual(
                    [key for key in registry if key.startswith("custom.")], []
                )

    def test_structural_review_loads_into_both_registries_when_allowed(self):
        """The premise for the test above: these registries can hold a custom."""
        config = {"providers": [declaration("Good")]}
        base, target, detail = _resolved_registries(
            config, config, allow_custom_providers=True
        )
        self.assertIsNone(detail)
        self.assertIn("custom.good", base)
        self.assertIn("custom.good", target)
        self.assertIsNot(base["custom.good"], target["custom.good"])

    def _drifted_lock(self, scene: Scenario) -> dict:
        lockfile = generate_lockfile(
            scene.config, scene.root, source="head", allow_custom_providers=True
        )
        scene.file("svc/api.json", '{"v": 2}\n')
        scene.git("add", "--all")
        scene.git("commit", "-q", "-m", "change the boundary")
        return lockfile

    def test_the_provider_explanation_is_present_when_nothing_failed_to_load(self):
        """The premise for the suppression test: an explanation is produced."""
        with _custom_provider_scenario([declaration("Good")]) as scene:
            lockfile = self._drifted_lock(scene)
            result = analyze_component_drift(
                scene.config,
                lockfile,
                scene.root,
                "svc",
                source="head",
                allow_custom_providers=True,
            )
        self.assertIsNotNone(result)
        self.assertIn("boundary", result["changes"])
        self.assertEqual(result["provider_explanation"], "custom.good boundary changed")

    def test_a_load_error_suppresses_the_explanation_for_a_provider_that_loaded(self):
        """A second declaration failing hides the first one's explanation.

        Reaching this guard needs a provider that constructs once and then
        refuses, because `analyze_component_drift` computes current
        fingerprints before it gets here and `generate_lockfile` raises on any
        load error. `FlakySecond` survives that first load and fails the
        second, which is the only shape in which `load_errors` is non-empty
        while `get_provider("custom.good")` still answers.
        """
        with _custom_provider_scenario([declaration("Good")]) as scene:
            lockfile = self._drifted_lock(scene)
            scene.config["providers"] = [
                declaration("Good"),
                declaration("FlakySecond"),
            ]
            FlakySecond.constructions = 0
            result = analyze_component_drift(
                scene.config,
                lockfile,
                scene.root,
                "svc",
                source="head",
                allow_custom_providers=True,
            )
        self.assertEqual(FlakySecond.constructions, 2)
        self.assertIsNotNone(result)
        self.assertIn("boundary", result["changes"])
        self.assertEqual(result["provider_explanation"], "")

    def test_a_deterministic_load_error_stops_drift_analysis_before_that_guard(self):
        """Pin the ordinary route, which never reaches the guard above."""
        with _custom_provider_scenario([declaration("Good")]) as scene:
            lockfile = self._drifted_lock(scene)
            scene.config["providers"] = [
                declaration("Good"),
                declaration("Missing"),
            ]
            result = analyze_component_drift(
                scene.config,
                lockfile,
                scene.root,
                "svc",
                source="head",
                allow_custom_providers=True,
            )
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# OBL-PROVIDERS-041: extension exits become ordinary load errors
# ---------------------------------------------------------------------------

EXITING_PROVIDER_MODULE = "_boundver_chunk01_exiting_provider"

EXITING_PROVIDER_SOURCE = '''"""A provider dependency that calls sys.exit during import."""

import sys

sys.exit(0)


class Provider:
    name = "custom.exiting"
    version = "1"
'''

#: In-memory extension sites that must return load errors, not process-control
#: exceptions. The module-import case has its own filesystem-backed test.
BASE_EXCEPTION_SITES: Dict[str, dict] = {
    "class attribute read": {
        "module": EXITING_TRAP_MODULE,
        "class": "Provider",
    },
    "constructor": declaration("InterruptedConstructor"),
    "name property": declaration("ExitingName"),
}


class BaseExceptionBecomesLoadErrorTests(_RegistryIsolation):
    """OBL-PROVIDERS-041: extension failures always become error strings."""

    def test_an_ordinary_exception_at_each_site_becomes_an_error_string(self):
        """The premise: these three sites do convert a failure into a string.

        Without this the three pinning tests below would only show that
        something went wrong, not that `Exception` and `BaseException` are
        treated differently at the same three guards.
        """
        for label, entry in (
            ("name property", declaration("UnreadableName")),
            ("constructor", declaration("BadConstructor")),
            ("module import", {"module": ABSENT_MODULE, "class": "Provider"}),
            ("class attribute", {"module": TRAP_MODULE, "class": "Provider"}),
        ):
            with self.subTest(site=label):
                errors = load_custom_providers(
                    [entry], True, registry=create_registry()
                )
                self.assertEqual(len(errors), 1, errors)
                self.assertIsInstance(errors[0], str)

    def test_a_name_property_raising_system_exit_becomes_an_error(self):
        errors = load_custom_providers(
            [declaration("ExitingName")], True, registry=create_registry()
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("attribute 'name' could not be read", errors[0])

    def test_a_constructor_raising_keyboard_interrupt_becomes_an_error(self):
        errors = load_custom_providers(
            [declaration("InterruptedConstructor")],
            True,
            registry=create_registry(),
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("Failed to instantiate", errors[0])
        self.assertIn("KeyboardInterrupt", errors[0])

    def test_a_class_attribute_read_that_exits_becomes_an_error(self):
        errors = load_custom_providers(
            [{"module": EXITING_TRAP_MODULE, "class": "Provider"}],
            True,
            registry=create_registry(),
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("Failed to read", errors[0])

    def test_a_module_whose_import_exits_becomes_an_error(self):
        with Scenario("exiting") as scene:
            (scene.root / (EXITING_PROVIDER_MODULE + ".py")).write_text(
                EXITING_PROVIDER_SOURCE, encoding="utf-8"
            )
            sys.path.insert(0, str(scene.root))
            try:
                errors = load_custom_providers(
                    [{"module": EXITING_PROVIDER_MODULE, "class": "Provider"}],
                    True,
                    registry=create_registry(),
                )
            finally:
                sys.path.remove(str(scene.root))
                sys.modules.pop(EXITING_PROVIDER_MODULE, None)
        self.assertEqual(len(errors), 1)
        self.assertIn("Failed to import provider module", errors[0])

    def test_the_loader_docstring_still_promises_that_it_raises_nothing(self):
        """The implementation and its documented no-raise contract stay aligned."""
        self.assertIn("Raises nothing", providers.load_custom_providers.__doc__)

    def test_load_custom_providers_returns_a_list_for_every_declaration(self):
        """All in-memory extension failures preserve the loader contract."""
        outcomes = {
            label: load_custom_providers([entry], True, registry=create_registry())
            for label, entry in BASE_EXCEPTION_SITES.items()
        }
        self.assertTrue(all(type(outcome) is list for outcome in outcomes.values()))
        self.assertTrue(all(len(outcome) == 1 for outcome in outcomes.values()))

    def test_verify_fails_closed_when_a_provider_module_exits(self):
        """An authorized provider cannot turn drift into a silent success."""
        with Scenario("greenci") as scene:
            scene.component(
                "svc", path="svc", provider="path-hash", boundary=["api.json"]
            )
            scene.file("svc/api.json", '{"v": 1}\n')
            scene.commit("content")
            self.assertEqual(run_cli(scene.root, "generate").returncode, 0)
            scene.git("add", "--all")
            scene.git("commit", "-q", "-m", "lock")
            clean = run_cli(scene.root, "verify")
            self.assertEqual(clean.returncode, 0, clean.stderr)
            self.assertIn("Lockfile is up to date.", clean.stdout)

            scene.file("svc/api.json", '{"v": 999}\n')
            scene.file(EXITING_PROVIDER_MODULE + ".py", EXITING_PROVIDER_SOURCE)
            scene.config["providers"] = [
                {"module": EXITING_PROVIDER_MODULE, "class": "Provider"}
            ]
            scene.write_config()
            scene.git("add", "--all")
            scene.git("commit", "-q", "-m", "drift plus an exiting provider module")

            refused = run_cli(scene.root, "verify")
            self.assertEqual(refused.returncode, 2, refused.stdout)
            self.assertIn("LOCKFILE OUT OF DATE", refused.stdout)

            exited = run_cli(scene.root, "verify", "--allow-custom-providers")
            self.assertEqual(exited.returncode, 2)
            self.assertTrue(exited.stdout or exited.stderr)
            self.assertIn(
                "Failed to import provider module", exited.stdout + exited.stderr
            )


# ---------------------------------------------------------------------------
# OBL-PROVIDERS-020: a repository may declare Python, only a caller may run it
# ---------------------------------------------------------------------------

WITNESS_MODULE = "_boundver_import_witness"
WITNESS_MARKER = "IMPORT-WITNESS.txt"

WITNESS_SOURCE = f'''"""A declared provider whose import leaves a mark on the filesystem."""

from pathlib import Path

with Path(__file__).with_name("{WITNESS_MARKER}").open("a", encoding="utf-8") as handle:
    handle.write("imported\\n")


class WitnessProvider:
    name = "custom.witness"
    version = "1"

    def resolve(self, ctx):
        from boundver.providers import ResolvedBoundary

        entries = []
        for repo_rel in sorted(ctx.list_files(ctx.component_path + "/contract.json")):
            entries.append(("file:contract.json", ctx.read_file(repo_rel)))
        return ResolvedBoundary(entries=entries)
'''

#: Every subcommand that accepts `--allow-custom-providers`, with the arguments
#: that make it run in the fixture repository and what it does there. The keys
#: are compared against the parser at runtime, so a ninth such command fails
#: this table rather than slipping past it. The "<base>" and "<target>"
#: placeholders are filled with the fixture's commits.
#:
#: Four fields per row: the arguments, the exit code without the flag, whether
#: the refusal sentence appears without it, and the number of imports the same
#: command performs WITH it. That last field is what keeps the eight zeros
#: honest. An assertion that a command did not import proves nothing unless the
#: command imports at all, and a future change that stopped one of these loading
#: providers altogether would otherwise leave its row green and empty. Every
#: zero here is paired with the same command's one.
#:
#: The exit codes are worth reading: only generate, verify, why and review
#: refuse. A config declaring custom providers is *valid*, so check-config and
#: validate-config succeed, and explain answers from the lockfile without
#: needing a provider. Exit 2 is boundver's generic error code, so the refusal
#: sentence is asserted alongside it - otherwise a command broken for an
#: unrelated reason would still be green on both the code and the zero. The
#: refusal field is not simply the exit code repeated: status exits 0 and prints
#: the refusal anyway, which is the one row where the two disagree. Streams
#: observed: stderr for generate, why and review; stdout for verify and status.
TRUST_COMMANDS: Dict[str, Tuple[Tuple[str, ...], int, bool, int]] = {
    "generate": (("generate", "--out", "scratch.lock.json"), 2, True, 1),
    "verify": (("verify",), 2, True, 1),
    "validate-config": (("validate-config",), 0, False, 1),
    "check-config": (("check-config",), 0, False, 1),
    "status": (("status",), 0, True, 1),
    "explain": (("explain", "svc"), 0, False, 1),
    "why": (("why", "svc"), 2, True, 1),
    "review": (("review", "--base", "<base>", "--target", "<target>"), 2, True, 1),
}

#: Fields a repository config could plausibly use to claim the authority, and
#: the first line the CLI prints when it finds one. Neither is accepted, and the
#: top-level spelling gets a message written for exactly this attack.
PLANTED_AUTHORITY = {
    "top level": (
        {"allow_custom_providers": True},
        "Top-level 'allow_custom_providers' is not supported: repository config "
        "cannot authorize Python imports; pass --allow-custom-providers or set "
        "BOUNDVER_ALLOW_CUSTOM_PROVIDERS=1 in trusted automation",
    ),
    "under defaults": (
        {"defaults": {"allow_custom_providers": True}},
        "Unknown field in defaults: allow_custom_providers",
    ),
}


def accepted_config_fields() -> set:
    """Every field name the config contract accepts, read from the contract.

    Every set-valued name in `_config_contract` ends in `_FIELDS` today, so this
    sweep covers the whole declared vocabulary rather than a chosen corner of it.
    """
    from boundver import _config_contract

    accepted = set()
    for name in dir(_config_contract):
        if name.endswith("_FIELDS"):
            accepted |= set(getattr(_config_contract, name))
    return accepted


def commands_that_accept_the_flag() -> List[str]:
    """The surface, read from the parser production uses."""
    parser = build_parser(version="0", epilog="")
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(
                name
                for name, sub in action.choices.items()
                if any(
                    "--allow-custom-providers" in item.option_strings
                    for item in sub._actions
                )
            )
    raise AssertionError("the parser declares no subcommands")


class CustomProviderTrustBoundaryTests(unittest.TestCase):
    """OBL-PROVIDERS-020: checking out a branch never grants it the interpreter."""

    scene: Scenario
    base: str
    target: str

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario("witness")
        scene = cls.scene
        scene.component(
            "svc", path="svc", provider="custom.witness", boundary=["contract.json"]
        )
        scene.config["providers"] = [
            {"module": WITNESS_MODULE, "class": "WitnessProvider"}
        ]
        scene.file("svc/contract.json", '{"v": 1}\n')
        scene.file(WITNESS_MODULE + ".py", WITNESS_SOURCE)
        scene.commit("declare the provider")
        generated = run_cli(scene.root, "generate", "--allow-custom-providers")
        assert generated.returncode == 0, generated.stderr
        scene.git("add", "--all")
        scene.git("commit", "-q", "-m", "lock")
        cls.base = scene.head()
        scene.file("svc/contract.json", '{"v": 2}\n')
        scene.git("add", "--all")
        scene.git("commit", "-q", "-m", "change the contract")
        regenerated = run_cli(scene.root, "generate", "--allow-custom-providers")
        assert regenerated.returncode == 0, regenerated.stderr
        scene.git("add", "--all")
        scene.git("commit", "-q", "-m", "relock")
        cls.target = scene.head()

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()

    def setUp(self):
        self._clear_witness()
        self._config_text = (self.scene.root / "boundary.config.json").read_text(
            encoding="utf-8"
        )

    def tearDown(self):
        (self.scene.root / "boundary.config.json").write_text(
            self._config_text, encoding="utf-8"
        )
        self._clear_witness()

    def _clear_witness(self) -> None:
        marker = self.scene.root / WITNESS_MARKER
        if marker.exists():
            marker.unlink()

    def _imports(self) -> int:
        marker = self.scene.root / WITNESS_MARKER
        if not marker.exists():
            return 0
        return marker.read_text(encoding="utf-8").count("imported")

    def _argv(self, args: Tuple[str, ...]) -> Tuple[str, ...]:
        return tuple(
            self.base if item == "<base>" else self.target if item == "<target>" else item
            for item in args
        )

    def test_the_witness_records_an_import_when_the_caller_authorizes_one(self):
        """The premise for every absence below: the marker really is written.

        A test that asserted "no import happened" against a module nothing ever
        imports would pass for the wrong reason forever. This runs the same
        repository, the same config and the same command with the flag added,
        and the file appears.
        """
        result = run_cli(
            self.scene.root,
            "generate",
            "--allow-custom-providers",
            "--out",
            "scratch.lock.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._imports(), 1)

    def test_the_table_lists_every_command_that_can_be_given_the_authority(self):
        self.assertEqual(sorted(TRUST_COMMANDS), commands_that_accept_the_flag())

    def test_every_command_that_accepts_the_flag_imports_once_when_given_it(self):
        """The premise for every zero in the table, one per command.

        Without this, a command that stopped loading custom providers
        altogether would keep a green row asserting it did not import - an
        absence over a mechanism that is no longer there. Running the same
        eight commands in the same repository with the flag added makes each
        zero the counterpart of a one that was observed for the same command.
        """
        for name, (args, _exit, _refusal, expected_imports) in sorted(
            TRUST_COMMANDS.items()
        ):
            with self.subTest(command=name):
                self._clear_witness()
                result = run_cli(
                    self.scene.root, *self._argv(args), "--allow-custom-providers"
                )
                self.assertEqual(
                    self._imports(),
                    expected_imports,
                    result.stdout + result.stderr,
                )
                self.assertEqual(
                    result.returncode, 0, result.stdout + result.stderr
                )

    def test_no_command_imports_the_declared_module_without_the_flag(self):
        for name, (args, expected_exit, refuses, _imports) in sorted(
            TRUST_COMMANDS.items()
        ):
            with self.subTest(command=name):
                self._clear_witness()
                result = run_cli(self.scene.root, *self._argv(args))
                reported = result.stdout + result.stderr
                self.assertEqual(self._imports(), 0, reported)
                self.assertEqual(result.returncode, expected_exit, reported)
                # Exit 2 is generic, so pin it to the reason it was produced.
                self.assertEqual(REFUSAL_SENTENCE in reported, refuses, reported)

    def test_review_fails_closed_on_a_config_that_declares_custom_providers(self):
        result = run_cli(
            self.scene.root, "review", "--base", self.base, "--target", self.target
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self._imports(), 0)
        self.assertIn(
            "base commit",
            result.stderr,
        )
        self.assertIn("has unreconciled drift", result.stderr)
        self.assertIn(
            "Checkpoint search was skipped because the endpoint declares custom providers",
            result.stderr,
        )
        self.assertIn(TRUST_ERROR, result.stderr)

    def test_review_proceeds_once_trusted_automation_passes_the_flag(self):
        """The premise for fail-closed: the range is reviewable, it just is not

        reviewed without authority. Without this the refusal above could be a
        broken fixture rather than a policy.
        """
        result = run_cli(
            self.scene.root,
            "review",
            "--base",
            self.base,
            "--target",
            self.target,
            "--allow-custom-providers",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._imports(), 1)
        self.assertIn("BOUNDVER RANGE REVIEW", result.stdout)

    def test_load_config_returns_the_declaration_without_importing_it(self):
        previous = Path.cwd()
        sys.path.insert(0, str(self.scene.root))
        try:
            os.chdir(self.scene.root)
            config = boundver.load_config()
        finally:
            os.chdir(previous)
            sys.path.remove(str(self.scene.root))
            sys.modules.pop(WITNESS_MODULE, None)
        self.assertEqual(
            config["providers"],
            [{"module": WITNESS_MODULE, "class": "WitnessProvider"}],
        )
        self.assertEqual(self._imports(), 0)

    def test_a_planted_authority_field_is_refused_rather_than_honoured(self):
        config_file = self.scene.root / "boundary.config.json"
        for label, (overlay, message) in sorted(PLANTED_AUTHORITY.items()):
            with self.subTest(field=label):
                self._clear_witness()
                payload = json.loads(self._config_text)
                payload.update(overlay)
                config_file.write_text(
                    json.dumps(payload, indent=2) + "\n", encoding="utf-8"
                )
                result = run_cli(self.scene.root, "validate-config")
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(message, result.stdout)
                self.assertEqual(self._imports(), 0)

    def test_no_config_field_is_spelled_like_an_authorization_switch(self):
        """A tripwire for the obvious spellings, and not the proof of anything.

        Read this for what it is: a five-token grep over the accepted
        vocabulary. It would catch `allow_python` or `trusted`, and it would
        say nothing at all about a field named `run_repo_python` or
        `plugin_mode`. The guarantee that no config field of ANY name can grant
        the authority is not here - it is in
        `test_no_accepted_config_field_can_grant_the_authority_whatever_it_is_called`
        below, which plants every accepted name in turn and shows the answer
        never moves, because `_resolve_allow_custom` at core.py:221 reads only
        `getattr(args, ...)` and never consults the config at all.
        """
        accepted = accepted_config_fields()
        self.assertIn("providers", accepted)
        suspicious = sorted(
            field
            for field in accepted
            if any(
                token in field.lower()
                for token in ("allow", "trust", "authoriz", "exec", "import")
            )
        )
        self.assertEqual(suspicious, [])

    def test_no_accepted_config_field_can_grant_the_authority_whatever_it_is_called(
        self,
    ):
        """The enumeration the sweep above only gestures at, done exhaustively.

        Every name the config contract accepts is planted at the top level and
        under `defaults`, set to True, one at a time. A caller that did not pass
        the flag still gets False for all of them. This holds for a name nobody
        has thought of yet as well, since the function never reads the config,
        but running it over the real vocabulary is what turns that from a claim
        about a line of source into an observation.

        The planted names include the two spellings from `PLANTED_AUTHORITY`,
        which the contract *rejects* rather than accepts and which
        `accepted_config_fields` therefore does not return. Without them this
        test would have a hole exactly where the attack is most obvious: a
        `_resolve_allow_custom` that honoured a top-level
        `allow_custom_providers` would be caught by
        `test_resolve_allow_custom_reads_the_caller_and_ignores_the_repository`
        but not by an enumeration of the accepted vocabulary alone.
        """
        planted = accepted_config_fields() | {"allow_custom_providers", "trusted"}
        self.assertGreaterEqual(len(planted), 20, planted)
        without_flag = argparse.Namespace(allow_custom_providers=False)
        for field in sorted(planted):
            with self.subTest(field=field):
                self.assertIs(
                    core._resolve_allow_custom(without_flag, {field: True}), False
                )
                self.assertIs(
                    core._resolve_allow_custom(
                        without_flag, {"defaults": {field: True}}
                    ),
                    False,
                )

    def test_resolve_allow_custom_reads_the_caller_and_ignores_the_repository(self):
        """Pin core.py:215 directly: the config is not an argument to the answer."""
        hostile = {
            "allow_custom_providers": True,
            "trusted": True,
            "providers": [{"module": WITNESS_MODULE, "class": "WitnessProvider"}],
            "defaults": {"allow_custom_providers": True},
        }
        for flag in (False, True):
            with self.subTest(flag=flag):
                args = argparse.Namespace(allow_custom_providers=flag)
                self.assertIs(core._resolve_allow_custom(args, hostile), flag)
                self.assertIs(core._resolve_allow_custom(args, {}), flag)
        # A command whose parser never declared the flag gets False, not the
        # config's opinion.
        self.assertIs(
            core._resolve_allow_custom(argparse.Namespace(), hostile), False
        )


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-127: where `--source head` starts reading, to the commit
# ---------------------------------------------------------------------------


def _relock(scene: Scenario, message: str) -> Tuple[str, dict]:
    """Regenerate the lock from the committed tree and commit it on its own."""
    lockfile = scene.generate()
    (scene.root / LOCK).write_text(
        json.dumps(lockfile, indent=2) + "\n", encoding="utf-8"
    )
    scene.git("add", "--all")
    scene.git("commit", "-q", "-m", message)
    return scene.head(), lockfile


def _edit(scene: Scenario, path: str, text: str, message: str) -> str:
    scene.file(path, text)
    scene.git("add", "--all")
    scene.git("commit", "-q", "-m", message)
    return scene.head()


def _two_component_scenario(name: str) -> Scenario:
    scene = Scenario(name)
    scene.component("svc", path="svc", provider="path-hash", boundary=["api.json"])
    scene.component("web", path="web", provider="path-hash", boundary=["api.json"])
    scene.file("svc/api.json", '{"v": 1}\n')
    scene.file("svc/impl.py", "VALUE = 1\n")
    scene.file("web/api.json", '{"v": 1}\n')
    scene.commit("initial content")
    return scene


class ComponentLockHistoryBaseTests(unittest.TestCase):
    """OBL-GIT-SOURCE-127: the oldest commit of the newest matching run."""

    unrelated: Scenario
    aba: Scenario

    @classmethod
    def setUpClass(cls):
        # History one: the lock is rewritten for another component, and then
        # reformatted, after svc's entry was introduced.
        cls.unrelated = _two_component_scenario("unrelated")
        scene = cls.unrelated
        cls.introduced, first = _relock(scene, "lock: introduce both entries")
        _edit(scene, "web/api.json", '{"v": 2}\n', "web contract change")
        cls.web_rewrite, second = _relock(scene, "lock: web only")
        # Same values, different bytes: a commit that touches the lock without
        # changing any component entry.
        reformatted = json.loads((scene.root / LOCK).read_text(encoding="utf-8"))
        cls.reformat = _edit(
            scene,
            LOCK,
            json.dumps(reformatted, indent=4) + "\n",
            "lock: reformat only",
        )
        cls.unrelated_lock = json.loads(
            (scene.root / LOCK).read_text(encoding="utf-8")
        )
        cls.unrelated_head = scene.head()
        cls.first_lock, cls.second_lock = first, second

        # History two: svc's entry is present, replaced, and restored, and then
        # the lock moves once more for an unrelated component.
        cls.aba = _two_component_scenario("aba")
        scene = cls.aba
        cls.a1, _ = _relock(scene, "lock: entry A")
        _edit(scene, "svc/api.json", '{"v": 2}\n', "svc contract to B")
        cls.b, _ = _relock(scene, "lock: entry B")
        _edit(scene, "svc/api.json", '{"v": 1}\n', "svc contract back to A")
        cls.a2, _ = _relock(scene, "lock: entry A again")
        _edit(scene, "web/api.json", '{"v": 5}\n', "web contract change")
        cls.after, _ = _relock(scene, "lock: web only")
        cls.drift = _edit(
            scene, "svc/impl.py", "VALUE = 2\n", "svc implementation drift"
        )
        cls.aba_lock = json.loads((scene.root / LOCK).read_text(encoding="utf-8"))
        cls.aba_head = scene.head()

    @classmethod
    def tearDownClass(cls):
        cls.unrelated.close()
        cls.aba.close()

    def _lock_commits(self, scene: Scenario, head: str) -> List[str]:
        output = scene.git("rev-list", "--first-parent", head, "--", LOCK)
        return output.splitlines() if output else []

    def _base(self, scene: Scenario, head: str, lock: dict, component: str):
        return _component_lock_history_base(
            scene.root, head, LOCK, component, lock["components"][component]
        )

    def test_the_fixture_really_did_leave_svc_untouched_across_the_rewrite(self):
        """The premise for history one: the two lock commits differ, and they

        differ for web and not for svc. If both entries had moved, the base
        landing on the introducing commit would prove nothing about which
        component the walk followed.
        """
        self.assertEqual(
            self.first_lock["components"]["svc"],
            self.second_lock["components"]["svc"],
        )
        self.assertNotEqual(
            self.first_lock["components"]["web"],
            self.second_lock["components"]["web"],
        )
        self.assertEqual(
            self._lock_commits(self.unrelated, self.unrelated_head),
            [self.reformat, self.web_rewrite, self.introduced],
        )

    def test_a_rewrite_for_another_component_does_not_become_the_base(self):
        base, origin = self._base(
            self.unrelated, self.unrelated_head, self.unrelated_lock, "svc"
        )
        self.assertEqual(base, self.introduced)
        self.assertNotEqual(base, self.web_rewrite)
        self.assertEqual(
            origin, "commit that introduced the current lock entry for svc"
        )

    def test_a_commit_that_only_reformats_the_lock_does_not_become_the_base(self):
        base, _origin = self._base(
            self.unrelated, self.unrelated_head, self.unrelated_lock, "svc"
        )
        self.assertNotEqual(base, self.reformat)
        self.assertEqual(base, self.introduced)

    def test_the_same_history_gives_a_different_base_for_the_other_component(self):
        """The premise that the answer is derived and not constant: web's entry

        did move in the rewrite commit, so web's base is that commit while
        svc's is two commits older, out of one repository and one walk.
        """
        base, origin = self._base(
            self.unrelated, self.unrelated_head, self.unrelated_lock, "web"
        )
        self.assertEqual(base, self.web_rewrite)
        self.assertEqual(
            origin, "commit that introduced the current lock entry for web"
        )

    def test_an_a_b_a_history_resolves_to_the_newest_run_not_the_first_sighting(self):
        base, origin = self._base(self.aba, self.aba_head, self.aba_lock, "svc")
        self.assertEqual(base, self.a2)
        self.assertNotEqual(base, self.a1)
        self.assertNotEqual(base, self.b)
        self.assertNotEqual(base, self.after)
        self.assertEqual(
            origin, "commit that introduced the current lock entry for svc"
        )

    def test_the_restored_entry_really_is_the_entry_that_was_replaced(self):
        """The premise for A-B-A: the third state equals the first exactly.

        Compared through the lock blobs Git stored, because the walk compares
        parsed historical entries and not the working tree.
        """
        a1_lock = json.loads(self.aba.blob(LOCK, self.a1).decode("utf-8"))
        b_lock = json.loads(self.aba.blob(LOCK, self.b).decode("utf-8"))
        a2_lock = json.loads(self.aba.blob(LOCK, self.a2).decode("utf-8"))
        self.assertEqual(
            a1_lock["components"]["svc"], a2_lock["components"]["svc"]
        )
        self.assertNotEqual(
            a1_lock["components"]["svc"], b_lock["components"]["svc"]
        )

    def test_the_chosen_base_is_always_a_commit_that_touched_the_lock(self):
        cases = {
            "unrelated/svc": (self.unrelated, self.unrelated_head, self.unrelated_lock, "svc"),
            "unrelated/web": (self.unrelated, self.unrelated_head, self.unrelated_lock, "web"),
            "aba/svc": (self.aba, self.aba_head, self.aba_lock, "svc"),
            "aba/web": (self.aba, self.aba_head, self.aba_lock, "web"),
        }
        for label, (scene, head, lock, component) in sorted(cases.items()):
            with self.subTest(case=label):
                base, _origin = self._base(scene, head, lock, component)
                self.assertIn(base, self._lock_commits(scene, head))

    def test_the_command_a_reviewer_runs_reports_that_same_commit(self):
        result = run_cli(
            self.aba.root, "why", "svc", "--source", "head", "--format", "json"
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["diagnostic_base"], self.a2)
        self.assertEqual(
            payload["diagnostic_base_origin"],
            "commit that introduced the current lock entry for svc",
        )
        self.assertEqual(
            payload["changed_files"], [{"path": "svc/impl.py", "status": "M"}]
        )

    def test_the_resolution_ladder_agrees_with_the_component_walk(self):
        snapshot = _capture_git_source_snapshot(self.aba.root, "head")
        self.assertEqual(
            _resolve_lock_history_base(
                self.aba.root,
                "head",
                snapshot,
                LOCK,
                component_name="svc",
                locked_component=self.aba_lock["components"]["svc"],
            ),
            (self.a2, "commit that introduced the current lock entry for svc"),
        )


class LockHistoryFallbackTests(unittest.TestCase):
    """The rung below the walk, which is what "must not become the base" avoids."""

    scene: Scenario

    @classmethod
    def setUpClass(cls):
        cls.scene = _two_component_scenario("fallback")
        cls.lock_commit, _ = _relock(cls.scene, "lock")
        cls.later = _edit(
            cls.scene, "svc/impl.py", "VALUE = 2\n", "a commit that leaves the lock alone"
        )
        cls.lock = json.loads((cls.scene.root / LOCK).read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()

    def test_an_entry_that_never_appears_makes_the_component_walk_decline(self):
        stranger = {"fingerprints": {"boundary": "0" * 64}}
        self.assertIsNone(
            _component_lock_history_base(
                self.scene.root, self.scene.head(), LOCK, "svc", stranger
            )
        )

    def test_declining_hands_the_answer_to_the_last_commit_that_touched_the_lock(self):
        """This is the origin the walk exists to avoid producing, and having it

        observable is what makes the assertions in the class above meaningful:
        the newest lock commit is a live alternative answer, not an impossible
        one.
        """
        snapshot = _capture_git_source_snapshot(self.scene.root, "head")
        stranger = {"fingerprints": {"boundary": "0" * 64}}
        self.assertEqual(
            _resolve_lock_history_base(
                self.scene.root,
                "head",
                snapshot,
                LOCK,
                component_name="svc",
                locked_component=stranger,
            ),
            (self.lock_commit, f"last commit that changed HEAD:{LOCK}"),
        )
        self.assertNotEqual(self.lock_commit, self.later)

    def test_a_caller_that_names_no_component_never_enters_the_walk(self):
        snapshot = _capture_git_source_snapshot(self.scene.root, "head")
        self.assertEqual(
            _resolve_lock_history_base(self.scene.root, "head", snapshot, LOCK),
            (self.lock_commit, f"last commit that changed HEAD:{LOCK}"),
        )

    def test_a_source_other_than_head_never_enters_the_walk(self):
        snapshot = _capture_git_source_snapshot(self.scene.root, "index")
        for source in ("index", "working-tree"):
            with self.subTest(source=source):
                self.assertEqual(
                    _resolve_lock_history_base(
                        self.scene.root,
                        source,
                        snapshot,
                        LOCK,
                        component_name="svc",
                        locked_component=self.lock["components"]["svc"],
                    ),
                    ("HEAD", "default for staged or working-tree diagnostics"),
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
