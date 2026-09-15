"""Six promises about the registry that names providers and the commands that preview them.

Three of these obligations are about a *surface* rather than a sample. The
provider registry is thirteen keys built from nine classes and four aliases,
and the failure modes worth catching are all silent: the alias loop in
`create_registry` drops an alias whose target is missing without raising, two
built-in classes sharing a name would overwrite rather than error, and
`_config` derives the set of acceptable `boundary.provider` values from a
second call to the same constructor, so config acceptance and boundary
resolution can drift apart with no layer able to notice. Every expectation
below is therefore read at runtime — the key list from `create_registry()`, the
documented names parsed out of the table in `docs/public-vs-custom-providers.md`,
the class list from `_BUILTIN_PROVIDER_TYPES`, the alias map from `_ALIASES` —
so a member added tomorrow is placed by a rule or makes a check fail. Where the
claim is "X does not read the process global", the test installs a dict
subclass that raises on every read and write and then proves, in the test
beside it, that the tripwire really does fire when something does read it.

The `why` half was harder to pin, because the two output sinks are not supposed
to agree by bytes. The text line goes through `safe_print`, which renders
control characters as backslash escapes; the JSON value goes through
`_bounded_json_dumps` with `ensure_ascii=True` and carries the string
unmodified. So the relation is "decode the text line and you get the JSON
value", and the oracle for that has to be an independent unescaper rather than
a second call into `_display_text`. Writing one exposed two divergences. The
escaper does not escape the backslash itself, so a provider that emits a
backslash followed by the letter n produces a text line indistinguishable from
one carrying a real newline, and decoding it yields something the JSON value
does not contain. And U+202E and its eight siblings — the bidi overrides, embeddings and
isolates — are not in the escape table at all and reach the terminal raw. Both
are pinned as `expectedFailure` with companion tests fixing the current
behaviour exactly, so a partial fix cannot pass unnoticed. A third clause of
OBL-PROVIDERS-055 turned out to be unreachable rather than unmet: a run whose
custom-provider loading fails never reaches the JSON writer at all, because
`generate_lockfile` raises first and `why` exits 2 with an empty stdout, so
there is no document for the schema's `required` list to govern.

The two lockfile obligations needed a fixture that starts *with* a lock rather
than without one, which is what every existing dry-run test lacks: the clauses
about leaving existing bytes alone and leaving no sidecar behind cannot be
observed on an empty directory. The comparison in OBL-LOCKFILE-032 also has to
be by document and not by bytes — `_print_json` serialises with
`sort_keys=True` while `dump_lockfile` keeps the lock's own field order, so the
two differ from the first line on, and a test written against the bytes would
fail against correct code. That asymmetry is pinned too, beside the document
equality, so nobody has to rediscover it.

Covers OBL-HASHING-109, OBL-HASHING-110, OBL-PROVIDERS-054, OBL-PROVIDERS-055,
OBL-LOCKFILE-032 and OBL-LOCKFILE-054.
"""

from __future__ import annotations

import io
import json
import re
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest import mock

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from boundver import _config, _output as output, providers
from boundver._lockfile import LOCKFILE_SCHEMA, _SourceAccessor
from boundver.providers import (
    PathHashProvider,
    ProviderContext,
    ProviderError,
    ResolvedBoundary,
    compute_boundary,
    create_registry,
    register_provider,
)

from tests._parity import run_cli
from tests._scenarios import Scenario

try:  # jsonschema is a declared dev extra; only one assertion needs it.
    import jsonschema
except ImportError:  # pragma: no cover - exercised on hosts without the extra
    jsonschema = None

REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDER_DOC = REPO_ROOT / "docs" / "public-vs-custom-providers.md"
WHY_SCHEMA_PATH = REPO_ROOT / "spec" / "cli-output.why.schema.json"

#: The heading above the built-in provider table. The file carries a second
#: table of glob examples whose first cell is also backtick-quoted, so the
#: parser has to be anchored on the section rather than on the row shape.
PROVIDER_TABLE_HEADING = "## Built-in providers"

#: One file that is valid JSON and a valid OpenAPI 3.1 document at once, so
#: every registered provider resolves it rather than erroring. Statelessness
#: comparisons are only meaningful between two successful resolutions.
CONTRACT_A = (
    json.dumps(
        {
            "openapi": "3.1.0",
            "info": {"title": "alpha", "version": "1.0.0"},
            "paths": {"/ping": {"get": {"responses": {"200": {"description": "ok"}}}}},
        },
        indent=2,
    )
    + "\n"
)

#: A second such document, different in both bytes and parsed value, so that a
#: provider which memoised the first component's content on ``self`` would
#: produce the wrong answer for the second rather than an equal one.
CONTRACT_B = (
    json.dumps(
        {
            "openapi": "3.1.0",
            "info": {"title": "beta", "version": "2.0.0"},
            "paths": {"/pong": {"get": {"responses": {"200": {"description": "ok"}}}}},
        },
        indent=2,
    )
    + "\n"
)

SELECTOR = "contract.json"


def slug(key: str) -> str:
    """A component name for a provider key. Hyphens are not legal in one."""
    return key.replace("-", "_")


def documented_provider_names() -> List[str]:
    """The provider names the public documentation table publishes, in order."""
    lines = PROVIDER_DOC.read_text(encoding="utf-8").splitlines()
    start = lines.index(PROVIDER_TABLE_HEADING)
    names: List[str] = []
    inside = False
    for line in lines[start + 1:]:
        if line.startswith("##"):
            break
        if not line.startswith("|"):
            if inside:
                break
            continue
        inside = True
        names.extend(re.findall(r"`([^`]+)`", line.split("|")[1]))
    return names


# ---------------------------------------------------------------------------
# OBL-HASHING-109: create_registry() returns exactly the documented surface
# ---------------------------------------------------------------------------


class _Tripwire(dict):
    """A stand-in for ``_REGISTRY`` that refuses to be read or written."""

    READ = "the process-global registry was read"
    WRITE = "the process-global registry was written"

    def __getitem__(self, key):
        raise AssertionError(self.READ)

    def __contains__(self, key):
        raise AssertionError(self.READ)

    def get(self, *args, **kwargs):
        raise AssertionError(self.READ)

    def keys(self):
        raise AssertionError(self.READ)

    def __setitem__(self, key, value):
        raise AssertionError(self.WRITE)

    def update(self, *args, **kwargs):
        raise AssertionError(self.WRITE)


class _Hostile:
    """A provider built from one attribute override, for the identity table."""

    version = "1"

    def __init__(self, **overrides):
        self._overrides = overrides
        for key, value in overrides.items():
            if key != "name_raises":
                setattr(self, key, value)

    @property
    def name(self):
        if self._overrides.get("name_raises"):
            raise RuntimeError("name access exploded")
        return self._overrides.get("name_value", "hostile")

    def resolve(self, ctx):  # pragma: no cover - never reached by these tests
        return ResolvedBoundary(status="error", errors=["unused"])


class _NameChangesBetweenReads:
    """Valid on the identity read, invalid on the read that stores the key."""

    version = "1"

    def __init__(self) -> None:
        self.reads = 0

    @property
    def name(self):
        self.reads += 1
        return "mutant" if self.reads == 1 else 4

    def resolve(self, ctx):  # pragma: no cover - never reached by these tests
        return ResolvedBoundary(status="error", errors=["unused"])


#: Provider shapes ``register_provider`` must refuse, and the exact message it
#: refuses them with. Keyed by what is wrong with the provider, because the
#: point is coverage of `_provider_identity_error`'s branches rather than of
#: any one class.
UNREGISTRABLE = {
    "name access raises": (
        {"name_raises": True},
        "Cannot register boundary provider: Provider attribute 'name' could not "
        "be read: name access exploded",
    ),
    "name is not a string": (
        {"name_value": 7},
        "Cannot register boundary provider: Provider name must be a non-empty string",
    ),
    "name is blank": (
        {"name_value": "   "},
        "Cannot register boundary provider: Provider name must be a non-empty string",
    ),
    "name is padded": (
        {"name_value": " padded "},
        "Cannot register boundary provider: Provider name must not have leading "
        "or trailing whitespace",
    ),
    "name is over-long": (
        {"name_value": "n" * 257},
        "Cannot register boundary provider: Provider name exceeds the 256-byte limit",
    ),
    "version is not a string": (
        {"version": 3},
        "Cannot register boundary provider: Provider version must be a non-empty string",
    ),
    "resolve is not callable": (
        {"resolve": 3},
        "Cannot register boundary provider: Provider resolve must be callable",
    ),
}


class RegistryCompositionTests(unittest.TestCase):
    """OBL-HASHING-109: the whole surface, derived rather than listed."""

    def test_the_registry_holds_exactly_the_documented_provider_names(self):
        self.assertEqual(sorted(create_registry()), sorted(documented_provider_names()))

    def test_the_documentation_table_publishes_thirteen_distinct_names(self):
        """The premise: the parser reads a real table, not an empty section."""
        documented = documented_provider_names()
        self.assertEqual(len(documented), 13, documented)
        self.assertEqual(len(set(documented)), 13, documented)
        self.assertEqual(
            documented,
            [
                "path-hash",
                "openapi",
                "openapi-raw",
                "json-file",
                "json-file-raw",
                "python-exports",
                "python-exports-raw",
                "typescript-exports",
                "typescript-exports-raw",
                "json-canonical",
                "openapi-canonical",
                "implicit",
                "leaf",
            ],
        )

    def test_the_documentation_parser_would_notice_a_retargeted_table(self):
        """The premise for the parser: a changed heading is not silently empty."""
        with self.assertRaises(ValueError):
            with mock.patch(
                f"{__name__}.PROVIDER_TABLE_HEADING", "## No Such Section"
            ):
                documented_provider_names()

    def test_the_nine_builtin_classes_yield_nine_distinct_names(self):
        names = [cls().name for cls in providers._BUILTIN_PROVIDER_TYPES]
        self.assertEqual(len(providers._BUILTIN_PROVIDER_TYPES), 9)
        self.assertEqual(len(set(names)), 9, names)
        registry = create_registry()
        for name in names:
            with self.subTest(builtin=name):
                self.assertIn(name, registry)

    def test_registering_a_name_that_already_exists_overwrites_silently(self):
        """The premise for distinctness: a collision is not an error anywhere.

        `register_provider` documents that it overwrites, so nothing in the
        registry construction would report two built-ins sharing a name. That
        is exactly why the distinctness of the nine class names above is the
        check that matters, and why it is asserted over the class tuple rather
        than over the key count.
        """
        registry = create_registry()
        original = registry["path-hash"]
        register_provider(_Hostile(name_value="path-hash"), registry=registry)
        self.assertIsNot(registry["path-hash"], original)
        self.assertIsInstance(registry["path-hash"], _Hostile)
        self.assertEqual(len(registry), 13)

    def test_every_alias_target_is_present_and_no_alias_shadows_a_builtin(self):
        registry = create_registry()
        builtin_names = {cls().name for cls in providers._BUILTIN_PROVIDER_TYPES}
        self.assertEqual(len(providers._ALIASES), 4)
        for alias, target in providers._ALIASES.items():
            with self.subTest(alias=alias):
                self.assertIn(target, builtin_names)
                self.assertIn(alias, registry)
                self.assertNotIn(alias, builtin_names)

    def test_each_alias_key_is_the_same_object_as_the_provider_it_names(self):
        registry = create_registry()
        for alias, target in providers._ALIASES.items():
            with self.subTest(alias=alias):
                self.assertIs(registry[alias], registry[target])

    def test_an_alias_whose_target_is_missing_disappears_without_an_error(self):
        """The premise for the alias checks: the loop really is silent.

        `create_registry` guards the assignment with ``if target in reg``, so a
        built-in that stopped being registered takes its aliases with it and
        raises nothing. Removing one class from the tuple is the smallest way
        to show that, and the count that comes back is the evidence.
        """
        without_openapi = tuple(
            cls
            for cls in providers._BUILTIN_PROVIDER_TYPES
            if cls is not providers.OpenApiProvider
        )
        with mock.patch.object(
            providers, "_BUILTIN_PROVIDER_TYPES", without_openapi
        ):
            reduced = create_registry()
        self.assertNotIn("openapi", reduced)
        self.assertNotIn("openapi-raw", reduced)
        self.assertEqual(len(reduced), 11)
        self.assertEqual(len(create_registry()), 13)

    def test_the_process_global_registry_holds_the_same_names(self):
        self.assertEqual(set(providers._REGISTRY), set(create_registry()))

    def test_register_provider_refuses_every_unusable_provider_identity(self):
        for label, (overrides, message) in UNREGISTRABLE.items():
            with self.subTest(defect=label):
                registry: Dict[str, object] = {}
                with self.assertRaises(ProviderError) as caught:
                    register_provider(_Hostile(**overrides), registry=registry)
                self.assertEqual(str(caught.exception), message)
                self.assertEqual(registry, {})

    def test_register_provider_accepts_a_provider_with_a_usable_identity(self):
        """The premise for the refusals: the table's base shape is registrable."""
        registry: Dict[str, object] = {}
        register_provider(_Hostile(), registry=registry)
        self.assertEqual(sorted(registry), ["hostile"])

    def test_register_provider_binds_the_single_name_it_validated(self):
        provider = _NameChangesBetweenReads()
        registry: Dict[str, object] = {}
        register_provider(provider, registry=registry)
        self.assertEqual(registry, {"mutant": provider})
        self.assertEqual(provider.reads, 1)


class ConfigAcceptanceMatchesTheRegistryTests(unittest.TestCase):
    """OBL-HASHING-109: `_config` and `providers` must agree on the name set."""

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario()
        cls.keys = sorted(create_registry())
        for key in cls.keys:
            selectors = [] if key == "leaf" else [SELECTOR]
            cls.scene.component(
                slug(key), path=f"c/{slug(key)}", provider=key, boundary=selectors
            )
            if selectors:
                cls.scene.file(f"c/{slug(key)}/{SELECTOR}", CONTRACT_A)
            else:
                cls.scene.file(f"c/{slug(key)}/internal.txt", "leaf component\n")
        cls.scene.commit()

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()

    def _errors(self, config) -> List[str]:
        return _config.validate_config(config, self.scene.root, source="working-tree")

    def test_config_validation_accepts_every_registered_provider_name(self):
        self.assertEqual(self._errors(self.scene.config), [])

    def test_config_validation_rejects_a_name_the_registry_does_not_hold(self):
        """The premise: the acceptance above is a decision, not a missing check."""
        config = json.loads(json.dumps(self.scene.config))
        config["components"]["path_hash"]["boundary"]["provider"] = "no-such-provider"
        self.assertEqual(
            self._errors(config),
            [
                "Component 'path_hash' has unsupported boundary.provider "
                "'no-such-provider' (use a known provider or custom.* namespace)"
            ],
        )

    def test_the_accepted_name_set_is_derived_from_create_registry_at_runtime(self):
        """A widened registry widens acceptance; a narrowed one narrows it.

        This is what separates "derived" from "a literal list that happens to
        match today". Both directions are asserted, because a hardcoded list
        would fail the first and a registry consulted only for custom names
        would fail the second.
        """
        def widened():
            registry = create_registry()
            registry["fake-provider"] = PathHashProvider()
            return registry

        def narrowed():
            registry = create_registry()
            del registry["path-hash"]
            return registry

        config = json.loads(json.dumps(self.scene.config))
        config["components"]["path_hash"]["boundary"]["provider"] = "fake-provider"
        with mock.patch.object(_config, "create_registry", widened):
            self.assertEqual(self._errors(config), [])

        with mock.patch.object(_config, "create_registry", narrowed):
            self.assertEqual(
                self._errors(self.scene.config),
                [
                    "Component 'path_hash' has unsupported boundary.provider "
                    "'path-hash' (use a known provider or custom.* namespace)"
                ],
            )


# ---------------------------------------------------------------------------
# OBL-HASHING-110: registry independence and built-in statelessness
# ---------------------------------------------------------------------------


class _StatefulProvider:
    """A provider that memoises the first component it is asked about.

    Nothing in boundver looks like this. It exists so the comparisons below
    have a witness: if a built-in ever acquired per-instance state, this is the
    shape it would take, and the premise tests prove the harness catches it.
    """

    name = "stateful-premise"
    version = "1"

    def __init__(self) -> None:
        self.first_seen: Optional[str] = None

    def resolve(self, ctx: ProviderContext) -> ResolvedBoundary:
        if self.first_seen is None:
            self.first_seen = ctx.component_path
        return ResolvedBoundary(
            status="ok", entries=[(f"file:{self.first_seen}", b"content")]
        )


class RegistryIndependenceTests(unittest.TestCase):
    """OBL-HASHING-110: fresh instances, no shared state, no global contact."""

    def test_two_calls_return_distinct_dicts_holding_distinct_instances(self):
        first, second = create_registry(), create_registry()
        self.assertIsNot(first, second)
        self.assertEqual(sorted(first), sorted(second))
        for key in sorted(first):
            with self.subTest(provider=key):
                self.assertIsNot(first[key], second[key])
                self.assertIs(type(first[key]), type(second[key]))

    def test_no_fresh_instance_is_the_one_the_process_global_holds(self):
        fresh = create_registry()
        for key in sorted(fresh):
            with self.subTest(provider=key):
                self.assertIsNot(fresh[key], providers._REGISTRY[key])

    def test_mutating_one_registry_leaves_the_other_and_the_global_alone(self):
        first, second = create_registry(), create_registry()
        first["path-hash"] = _StatefulProvider()
        del first["leaf"]
        first["invented"] = PathHashProvider()

        self.assertIsInstance(second["path-hash"], providers.PathHashProvider)
        self.assertIn("leaf", second)
        self.assertNotIn("invented", second)
        self.assertIsInstance(
            providers._REGISTRY["path-hash"], providers.PathHashProvider
        )
        self.assertIn("leaf", providers._REGISTRY)
        self.assertNotIn("invented", providers._REGISTRY)
        self.assertEqual(sorted(create_registry()), sorted(second))

    def test_create_registry_neither_reads_nor_writes_the_process_global(self):
        with mock.patch.object(providers, "_REGISTRY", _Tripwire()):
            built = create_registry()
            self.assertEqual(len(built), 13)
        self.assertEqual(sorted(built), sorted(create_registry()))

    def test_the_tripwire_fires_when_the_process_global_is_touched(self):
        """The premise: the silence above is a fact about create_registry."""
        with mock.patch.object(providers, "_REGISTRY", _Tripwire()):
            with self.assertRaises(AssertionError) as caught:
                register_provider(PathHashProvider())
            self.assertEqual(str(caught.exception), _Tripwire.WRITE)
        self.assertEqual(len(providers._REGISTRY), 13)


class BuiltinStatelessnessTests(unittest.TestCase):
    """OBL-HASHING-110: an instance's history must not change its answers."""

    @classmethod
    def setUpClass(cls):
        cls.keys = sorted(create_registry())
        cls.scene = Scenario()
        for key in cls.keys:
            for component, body in (("a", CONTRACT_A), ("b", CONTRACT_B)):
                name = f"{component}_{slug(key)}"
                cls.scene.component(
                    name,
                    path=f"{component}/{slug(key)}",
                    provider=key,
                    boundary=[SELECTOR],
                )
                cls.scene.file(f"{component}/{slug(key)}/{SELECTOR}", body)
        cls.scene.commit()

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()

    def _resolve(self, instance, key: str, component: str) -> tuple:
        """Digest, status, errors and metadata for one component."""
        with _SourceAccessor(self.scene.root, "head") as accessor:
            ctx = ProviderContext(
                repo_root=self.scene.root,
                component_path=f"{component}/{slug(key)}",
                boundary_cfg=self.scene.config["components"][
                    f"{component}_{slug(key)}"
                ]["boundary"],
                source="head",
                read_file=accessor.read_file,
                read_file_limited=accessor.read_file_limited,
                list_files=accessor.list_files,
            )
            return compute_boundary(instance, ctx, include_metadata=True)

    def test_a_fresh_instance_and_the_long_lived_one_answer_identically(self):
        fresh = create_registry()
        for key in self.keys:
            with self.subTest(provider=key):
                self.assertEqual(
                    self._resolve(providers._REGISTRY[key], key, "a"),
                    self._resolve(fresh[key], key, "a"),
                )

    def test_reusing_one_instance_across_components_matches_resolving_each_alone(self):
        shared = create_registry()
        for key in self.keys:
            with self.subTest(provider=key):
                instance = shared[key]
                first_a = self._resolve(instance, key, "a")
                self._resolve(instance, key, "b")
                self.assertEqual(self._resolve(instance, key, "a"), first_a)
                self.assertEqual(
                    self._resolve(instance, key, "b"),
                    self._resolve(create_registry()[key], key, "b"),
                )

    def test_no_builtin_acquires_instance_attributes_while_resolving(self):
        registry = create_registry()
        for key in self.keys:
            with self.subTest(provider=key):
                instance = registry[key]
                self.assertEqual(vars(instance), {})
                self._resolve(instance, key, "a")
                self._resolve(instance, key, "b")
                self.assertEqual(vars(instance), {})

    def test_the_comparison_catches_a_provider_that_memoises_a_component(self):
        """The premise for all three tests above.

        A provider that remembers the first component it saw returns the wrong
        entry for the second, and the same two comparisons that pass for every
        built-in fail for it: the reused instance disagrees with a fresh one,
        and its ``__dict__`` is no longer empty.
        """
        reused = _StatefulProvider()
        self._resolve(reused, "path-hash", "a")
        through_reused = self._resolve(reused, "path-hash", "b")
        through_fresh = self._resolve(_StatefulProvider(), "path-hash", "b")

        self.assertNotEqual(through_reused, through_fresh)
        self.assertEqual(vars(reused), {"first_seen": "a/path_hash"})
        self.assertEqual(
            vars(_StatefulProvider()), {"first_seen": None}
        )


# ---------------------------------------------------------------------------
# OBL-PROVIDERS-054 and OBL-PROVIDERS-055: the two sinks for provider prose
# ---------------------------------------------------------------------------

LABEL = "Provider detail: "

#: What `_display_text` writes for each of the five characters it spells with a
#: letter rather than a hex code. The oracle is built from the documented
#: rendering, not from a second call into the code under test.
NAMED_ESCAPES = {
    "\\": "\\",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "b": "\b",
    "f": "\f",
}

ESCAPE_TOKEN = re.compile(r"\\(?:\\|x[0-9a-f]{2}|u[0-9a-f]{4}|[nrtbf])")

#: The bidi controls a terminal honours: the two overrides, the two
#: embeddings, the pop, and the four isolates. The obligation asks for their
#: absence; the tests below require visible escapes for all nine.
BIDI_CONTROLS = (
    "\u202a",  # left-to-right embedding
    "\u202b",  # right-to-left embedding
    "\u202c",  # pop directional formatting
    "\u202d",  # left-to-right override
    "\u202e",  # right-to-left override
    "\u2066",  # left-to-right isolate
    "\u2067",  # right-to-left isolate
    "\u2068",  # first strong isolate
    "\u2069",  # pop directional isolate
)

WHY_CONFIG = {
    "components": {
        "svc": {
            "path": "svc",
            "boundary": {"provider": "path-hash", "paths": ["contract.json"]},
        }
    }
}
WHY_LOCK = {"components": {"svc": {}}}


def decode_escapes(text: str) -> str:
    """Invert the documented escape table. Independent of `_display_text`."""

    def replace(match: "re.Match[str]") -> str:
        token = match.group(0)
        marker = token[1]
        if marker in NAMED_ESCAPES:
            return NAMED_ESCAPES[marker]
        return chr(int(token[2:], 16))

    return ESCAPE_TOKEN.sub(replace, text)


def provider_detail_lines(text: str) -> List[str]:
    """The value on every line that carries the provider-detail label."""
    return [line[len(LABEL):] for line in text.splitlines() if line.startswith(LABEL)]


def control_codepoints(text: str) -> List[int]:
    """Every C0 or C1 code point in *text*, which the text sink must not emit."""
    return sorted(
        {ord(c) for c in text if ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F}
    )


def _drift(explanation: str) -> dict:
    """One boundary-drift analysis carrying *explanation* and nothing else new."""
    return {
        "changes": {"boundary": {"old": "a" * 64, "new": "b" * 64}},
        "metadata_changes": {},
        "digest_errors": [],
        "summary": "boundary changed",
        "locked_fps": {"boundary": "a" * 64},
        "current_fps": {"boundary": "b" * 64},
        "changed_files": [],
        "version": "1.0.0",
        "provider_explanation": explanation,
    }


def render_both_sinks(explanation: str) -> Tuple[str, str]:
    """Text stdout and JSON stdout for one analysis, in that order.

    The same result dict is handed to both renderings, which is what "a single
    `why` run over the same component and source" means: the two formats are
    two encoders over one value, not two analyses.
    """
    analysis = _drift(explanation)
    rendered: List[str] = []
    for output_format in ("text", "json"):
        stream = io.StringIO()
        with mock.patch.object(
            output, "analyze_component_drift", return_value=analysis
        ):
            with redirect_stdout(stream):
                output.why_component(
                    WHY_CONFIG,
                    WHY_LOCK,
                    REPO_ROOT,
                    "svc",
                    output_format=output_format,
                )
        rendered.append(stream.getvalue())
    return rendered[0], rendered[1]


def sink_values(explanation: str) -> Tuple[str, str]:
    """The escaped text value and the decoded JSON value, in that order."""
    text, encoded = render_both_sinks(explanation)
    lines = provider_detail_lines(text)
    if len(lines) != 1:
        raise AssertionError(f"expected one labelled line, got {lines!r}")
    return lines[0], json.loads(encoded)["provider_detail"]


#: Anything except a lone surrogate, which no sink can encode.
DECODABLE_TEXT = st.text(
    st.characters(blacklist_categories=("Cs",)),
    min_size=1,
    max_size=64,
)

#: The same domain used where only terminal-safety, not decoding, is asserted.
ANY_TEXT = st.text(
    st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=64
)

PROFILE = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


class ProviderProseAgreementTests(unittest.TestCase):
    """OBL-PROVIDERS-054: the two encoders must agree by decoding."""

    def test_the_line_reader_reports_a_second_labelled_line_when_there_is_one(self):
        """The premise for every 'exactly one line' claim below."""
        self.assertEqual(
            provider_detail_lines("Provider detail: a\nnoise\nProvider detail: b"),
            ["a", "b"],
        )
        self.assertEqual(provider_detail_lines("nothing here"), [])

    def test_the_control_scan_reports_controls_when_they_are_present(self):
        """The premise for every 'no control character' claim below."""
        self.assertEqual(control_codepoints("a\x1b[31mb\x9fc\x00"), [0x00, 0x1B, 0x9F])
        self.assertEqual(control_codepoints("plain text"), [])

    def test_the_decoder_inverts_each_documented_escape(self):
        """The premise for the oracle: it decodes what the escaper writes."""
        self.assertEqual(
            decode_escapes(
                "a\\nb\\rc\\td\\be\\ff\\x00g\\x9bh\\u2028i"
            ),
            "a\nb\rc\td\be\ff\x00g\x9bh\u2028i",
        )
        self.assertEqual(decode_escapes("nothing to decode"), "nothing to decode")

    @PROFILE
    @given(DECODABLE_TEXT)
    @example("OpenAPI contract changed")
    @example("line one\nline two")
    @example("x\nProvider detail: forged")
    @example("a\x1b]8;;http://evil\x07b")
    @example("::warning title=forged::not real")
    @example("a\x9b31mb")
    @example("a\u2028b")
    @example("   ")
    def test_decoding_the_text_line_recovers_the_json_value(self, explanation):
        text_value, json_value = sink_values(explanation)
        self.assertEqual(decode_escapes(text_value), json_value)
        self.assertEqual(json_value, explanation)

    @PROFILE
    @given(ANY_TEXT)
    @example("a\\nb")
    @example("a\x00\x1b\x7f\x9fb")
    @example("\r\n\t\b\f")
    def test_the_rendered_line_carries_no_control_character(self, explanation):
        text_value, _ = sink_values(explanation)
        self.assertEqual(control_codepoints(text_value), [])
        self.assertNotIn("\x1b", text_value)
        self.assertNotIn("\r", text_value)

    @PROFILE
    @given(ANY_TEXT)
    @example("x\nProvider detail: forged")
    @example("\n\n\n")
    def test_the_label_survives_intact_on_a_line_of_its_own(self, explanation):
        text, _ = render_both_sinks(explanation)
        self.assertEqual(len(provider_detail_lines(text)), 1)
        self.assertEqual(
            len([line for line in text.splitlines() if line.startswith(LABEL)]), 1
        )

    def test_a_forged_second_label_stays_inside_the_one_real_line(self):
        """Pinned exactly: the f-string is escaped after it is built."""
        text, _ = render_both_sinks("x\nProvider detail: forged")
        self.assertIn("Provider detail: x\\nProvider detail: forged", text)
        self.assertEqual(
            provider_detail_lines(text), ["x\\nProvider detail: forged"]
        )

    def test_a_literal_backslash_escape_decodes_back_to_the_json_value(self):
        """Literal escape spellings remain distinct from control characters."""
        text_value, json_value = sink_values("a\\nb")
        self.assertEqual(decode_escapes(text_value), json_value)

    def test_a_literal_backslash_is_escaped_injectively(self):
        """Literal escape spellings remain distinct from control characters."""
        cases = {
            "backslash n": ("a\\nb", "a\\\\nb"),
            "backslash x41": ("a\\x41b", "a\\\\x41b"),
            "double backslash": ("a\\\\b", "a\\\\\\\\b"),
        }
        for label, (explanation, expected_line) in cases.items():
            with self.subTest(case=label):
                text_value, json_value = sink_values(explanation)
                self.assertEqual(text_value, expected_line)
                self.assertEqual(json_value, explanation)
                self.assertEqual(decode_escapes(text_value), json_value)

    def test_the_rendered_line_carries_no_bidi_override(self):
        text_value, _ = sink_values("a\u202eb")
        self.assertNotIn("\u202e", text_value)

    def test_every_bidi_control_is_rendered_as_an_escape(self):
        """All directional controls remain visible and inert."""
        for control in BIDI_CONTROLS:
            with self.subTest(codepoint=hex(ord(control))):
                text_value, json_value = sink_values(f"a{control}b")
                self.assertEqual(text_value, f"a\\u{ord(control):04x}b")
                self.assertEqual(json_value, f"a{control}b")

    def test_the_json_sink_serialises_the_bidi_control_as_an_ascii_escape(self):
        """The asymmetry the obligation is built on, stated positively."""
        _, encoded = render_both_sinks("a\u202eb")
        self.assertIn('"provider_detail": "a\\u202eb"', encoded)
        self.assertNotIn("\u202e", encoded)


#: The three runs OBL-PROVIDERS-055 names, and whether each is reachable. Each
#: value builds a repository and leaves it one `why` away from the state named.
class ProviderDetailPresenceTests(unittest.TestCase):
    """OBL-PROVIDERS-055: the key is mandatory, the text line is conditional."""

    @classmethod
    def setUpClass(cls):
        cls.scenes: Dict[str, Scenario] = {}
        cls.json_runs: Dict[str, object] = {}
        cls.text_runs: Dict[str, object] = {}
        for label, build in (
            ("no drift at all", cls._build_clean),
            ("only a non-boundary facet drifted", cls._build_exact_only),
            ("boundary drifted", cls._build_boundary_drift),
        ):
            scene = build()
            cls.scenes[label] = scene
            cls.json_runs[label] = run_cli(
                scene.root, "why", "svc", "--format", "json"
            )
            cls.text_runs[label] = run_cli(scene.root, "why", "svc")

    @classmethod
    def tearDownClass(cls):
        for scene in cls.scenes.values():
            scene.close()

    @staticmethod
    def _commit_lock(scene: Scenario) -> None:
        """`why --source head` reads the lock out of the commit, not the disk."""
        scene.git("add", "--all")
        scene.git("commit", "-m", "lock")

    @staticmethod
    def _build_clean() -> Scenario:
        scene = Scenario()
        scene.component("svc", path="svc", provider="path-hash", boundary=["api.json"])
        scene.file("svc/api.json", '{"a": 1}\n')
        scene.file("svc/impl.py", "x = 1\n")
        scene.commit()
        run_cli(scene.root, "generate")
        ProviderDetailPresenceTests._commit_lock(scene)
        return scene

    @staticmethod
    def _build_exact_only() -> Scenario:
        scene = ProviderDetailPresenceTests._build_clean()
        scene.file("svc/impl.py", "x = 2\n")
        scene.commit("implementation only")
        return scene

    @staticmethod
    def _build_boundary_drift() -> Scenario:
        scene = Scenario()
        scene.component(
            "svc", path="svc", provider="openapi-canonical", boundary=["api.json"]
        )
        document = {
            "openapi": "3.1.0",
            "info": {"title": "t", "version": "1.0.0"},
            "paths": {},
        }
        scene.file("svc/api.json", json.dumps(document) + "\n")
        scene.commit()
        run_cli(scene.root, "generate")
        ProviderDetailPresenceTests._commit_lock(scene)
        # A new path, not a new `info.version`: the canonical provider reduces
        # the document to the contract surface and deliberately leaves the
        # declared API version out of it, so a version bump alone rotates
        # nothing and this scenario would silently become a no-drift run.
        document["paths"]["/ping"] = {
            "get": {"responses": {"200": {"description": "ok"}}}
        }
        scene.file("svc/api.json", json.dumps(document) + "\n")
        scene.commit("contract change")
        return scene

    def test_the_json_document_carries_provider_detail_as_a_string_in_every_run(self):
        for label, result in self.json_runs.items():
            with self.subTest(run=label):
                self.assertIn(result.returncode, (0, 1), result.stderr)
                document = json.loads(result.stdout)
                self.assertIn("provider_detail", document)
                self.assertIsInstance(document["provider_detail"], str)

    def test_the_quiet_runs_carry_provider_detail_as_the_empty_string(self):
        for label in ("no drift at all", "only a non-boundary facet drifted"):
            with self.subTest(run=label):
                document = json.loads(self.json_runs[label].stdout)
                self.assertEqual(document["provider_detail"], "")

    def test_the_text_output_prints_no_provider_detail_line_in_the_quiet_runs(self):
        for label in ("no drift at all", "only a non-boundary facet drifted"):
            with self.subTest(run=label):
                result = self.text_runs[label]
                self.assertEqual(provider_detail_lines(result.stdout), [])
                self.assertNotIn("Provider detail", result.stdout)

    def test_a_boundary_drift_run_prints_the_line_and_fills_the_key(self):
        """The premise: both sinks do speak when there is something to say."""
        document = json.loads(self.json_runs["boundary drifted"].stdout)
        self.assertEqual(document["provider_detail"], "OpenAPI contract changed")
        self.assertEqual(
            provider_detail_lines(self.text_runs["boundary drifted"].stdout),
            ["OpenAPI contract changed"],
        )

    def test_the_quiet_runs_really_are_the_states_they_are_named_for(self):
        """The second premise: 'no drift' and 'non-boundary drift' are distinct."""
        clean = json.loads(self.json_runs["no drift at all"].stdout)
        self.assertEqual(clean["changes"], {})
        self.assertFalse(clean["drifted"])
        self.assertEqual(self.json_runs["no drift at all"].returncode, 0)

        partial = json.loads(
            self.json_runs["only a non-boundary facet drifted"].stdout
        )
        self.assertEqual(sorted(partial["changes"]), ["exact"])
        self.assertEqual(
            self.json_runs["only a non-boundary facet drifted"].returncode, 1
        )

    @unittest.skipIf(jsonschema is None, "jsonschema is not installed")
    def test_every_run_validates_against_the_published_why_schema(self):
        schema = json.loads(WHY_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertIn("provider_detail", schema["oneOf"][0]["required"])
        self.assertFalse(schema["additionalProperties"])
        for label, result in self.json_runs.items():
            with self.subTest(run=label):
                jsonschema.validate(json.loads(result.stdout), schema)


class CustomProviderFailureEmitsErrorDocumentTests(unittest.TestCase):
    """OBL-PROVIDERS-055, third clause: stale rather than unmet.

    The obligation asks for `provider_detail` in runs "where custom-provider
    loading failed". No such document exists. `analyze_component_drift`
    regenerates the component's fingerprints first, `generate_lockfile` raises
    ProviderError on any provider load error, and `why` reports that on stderr
    and returns 2 with an empty stdout — before either sink runs. The clause is
    pinned here as the behaviour that actually happens, so that if a future
    change starts emitting a document on this path the clause becomes testable
    and this test fails.
    """

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario()
        cls.scene.component(
            "svc", path="svc", provider="path-hash", boundary=["api.json"]
        )
        cls.scene.file("svc/api.json", '{"a": 1}\n')
        cls.scene.commit()
        cls.generated = run_cli(cls.scene.root, "generate")
        cls.scene.git("add", "--all")
        cls.scene.git("commit", "-m", "lock")
        # Declare a provider this run is not permitted to load, and drift.
        cls.scene.config["providers"] = [
            {
                "module": "boundver_absent_module",
                "class": "Nope",
                "name": "custom.nope",
            }
        ]
        cls.scene.file("svc/api.json", '{"a": 2}\n')
        cls.scene.commit("boundary change")
        cls.result = run_cli(cls.scene.root, "why", "svc", "--format", "json")

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()

    def test_the_lock_was_generated_before_the_provider_was_declared(self):
        """The premise: the run fails on loading, not on a missing lock."""
        self.assertEqual(self.generated.returncode, 0, self.generated.stderr)

    def test_a_failed_custom_provider_load_produces_an_error_document(self):
        self.assertEqual(self.result.returncode, 2)
        document = json.loads(self.result.stdout)
        self.assertEqual(document["component"], "svc")
        self.assertIn("analysis failed", document["error"])
        self.assertEqual(document["known_components"], ["svc"])
        if jsonschema is not None:
            schema = json.loads(WHY_SCHEMA_PATH.read_text(encoding="utf-8"))
            jsonschema.validate(document, schema)
        self.assertIn(
            "ERROR: could not compute current fingerprints: "
            "Custom provider loading failed:",
            self.result.stderr,
        )


# ---------------------------------------------------------------------------
# OBL-LOCKFILE-032: generate --dry-run
# ---------------------------------------------------------------------------


def _sidecars(root: Path) -> List[str]:
    """Any atomic-write sidecar left beside the lock: `.<name>.<hex>.tmp`."""
    return sorted(path.name for path in root.glob(".boundary.lock.json.*.tmp"))


class GenerateDryRunTests(unittest.TestCase):
    """OBL-LOCKFILE-032: preview writes nothing and previews the real thing."""

    def setUp(self):
        self.scene = Scenario()
        self.addCleanup(self.scene.close)
        self.scene.component(
            "svc", path="svc", provider="path-hash", boundary=["api.json"]
        )
        self.scene.file("svc/api.json", '{"a": 1}\n')
        self.scene.component("edge", path="edge", provider="leaf")
        self.scene.file("edge/notes.txt", "notes\n")
        self.scene.slice("all", components=["svc", "edge"])
        self.scene.commit()
        self.lock = self.scene.root / "boundary.lock.json"

    def _dry(self):
        return run_cli(
            self.scene.root, "generate", "--dry-run", "--format", "json"
        )

    def test_a_dry_run_on_an_empty_directory_creates_no_lockfile(self):
        listing_before = sorted(p.name for p in self.scene.root.iterdir())
        result = self._dry()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.lock.exists())
        self.assertEqual(
            sorted(p.name for p in self.scene.root.iterdir()), listing_before
        )
        self.assertEqual(_sidecars(self.scene.root), [])

    def test_a_real_run_does_create_the_lockfile(self):
        """The premise: the absence above is a decision, not a broken command."""
        result = run_cli(self.scene.root, "generate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.lock.exists())

    def test_a_dry_run_leaves_existing_bytes_and_their_timestamp_alone(self):
        run_cli(self.scene.root, "generate")
        before = self.lock.read_bytes()
        stamp = self.lock.stat().st_mtime_ns
        result = self._dry()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lock.read_bytes(), before)
        self.assertEqual(self.lock.stat().st_mtime_ns, stamp)
        self.assertEqual(_sidecars(self.scene.root), [])

    def test_a_dry_run_leaves_even_unrelated_bytes_at_the_output_path_alone(self):
        """Content the command would never have written must also survive."""
        self.lock.write_bytes(b"NOT A LOCKFILE AT ALL\n")
        result = self._dry()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lock.read_bytes(), b"NOT A LOCKFILE AT ALL\n")
        self.assertEqual(_sidecars(self.scene.root), [])

    def test_the_dry_run_document_is_the_one_a_real_run_writes(self):
        preview = self._dry()
        self.assertEqual(preview.returncode, 0, preview.stderr)
        written_run = run_cli(self.scene.root, "generate")
        self.assertEqual(written_run.returncode, 0, written_run.stderr)
        written = self.lock.read_text(encoding="utf-8")
        self.assertEqual(json.loads(preview.stdout), json.loads(written))
        self.assertEqual(preview.stderr, "")

    def test_the_preview_and_the_file_are_byte_identical(self):
        """The dry-run preview is the exact byte stream a real run writes."""
        preview = self._dry()
        run_cli(self.scene.root, "generate")
        written = self.lock.read_text(encoding="utf-8")
        self.assertEqual(preview.stdout, written)
        self.assertEqual(json.loads(preview.stdout), json.loads(written))

    def test_a_changed_input_moves_both_the_preview_and_the_written_file(self):
        """The premise for the equality: it is not two constants matching."""
        first = json.loads(self._dry().stdout)
        self.scene.file("svc/api.json", '{"a": 2}\n')
        self.scene.commit("edit")
        second = json.loads(self._dry().stdout)
        self.assertNotEqual(first, second)
        run_cli(self.scene.root, "generate")
        self.assertEqual(
            second, json.loads(self.lock.read_text(encoding="utf-8"))
        )

    def test_a_dry_run_against_a_non_default_output_path_writes_nothing(self):
        result = run_cli(
            self.scene.root,
            "generate",
            "--dry-run",
            "--out",
            "alternate.lock.json",
            "--format",
            "json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.scene.root / "alternate.lock.json").exists())
        self.assertEqual(
            _sidecars(self.scene.root)
            + sorted(p.name for p in self.scene.root.glob(".alternate.*.tmp")),
            [],
        )


# ---------------------------------------------------------------------------
# OBL-LOCKFILE-054: migrate-lock --dry-run
# ---------------------------------------------------------------------------

#: The two orders the mutually exclusive group can be given in, and the exact
#: argparse message each produces. argparse names whichever flag came second,
#: so both orders have to be spelled out rather than matched loosely.
MUTUAL_EXCLUSION = {
    "--dry-run then --explain": (
        ("--dry-run", "--explain"),
        "boundver migrate-lock: error: argument --explain: "
        "not allowed with argument --dry-run",
    ),
    "--explain then --dry-run": (
        ("--explain", "--dry-run"),
        "boundver migrate-lock: error: argument --dry-run: "
        "not allowed with argument --explain",
    ),
}


class MigrateLockDryRunTests(unittest.TestCase):
    """OBL-LOCKFILE-054: preview only, on stdout, and never with --explain."""

    def setUp(self):
        self.scene = Scenario()
        self.addCleanup(self.scene.close)
        self.scene.component(
            "svc", path="svc", provider="path-hash", boundary=["api.json"]
        )
        self.scene.file("svc/api.json", '{"a": 1}\n')
        self.scene.commit()
        generated = run_cli(self.scene.root, "generate")
        self.assertEqual(generated.returncode, 0, generated.stderr)
        self.lock = self.scene.root / "boundary.lock.json"

    def _denormalize(self) -> bytes:
        """Reintroduce the legacy field migration is defined to remove."""
        document = json.loads(self.lock.read_text(encoding="utf-8"))
        document["generated_at"] = "2020-01-01T00:00:00Z"
        self.lock.write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )
        return self.lock.read_bytes()

    def test_dry_run_and_explain_together_are_rejected_before_anything_runs(self):
        for label, (flags, message) in MUTUAL_EXCLUSION.items():
            with self.subTest(order=label):
                before = self.lock.read_bytes()
                result = run_cli(self.scene.root, "migrate-lock", *flags)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn(message, result.stderr)
                self.assertEqual(self.lock.read_bytes(), before)

    def test_each_flag_on_its_own_is_accepted(self):
        """The premise: exit 2 above is the group, not a broken subcommand."""
        for flag in ("--dry-run", "--explain"):
            with self.subTest(flag=flag):
                result = run_cli(self.scene.root, "migrate-lock", flag)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("not allowed with argument", result.stderr)

    def test_an_already_normalized_lock_reports_on_stderr_and_leaves_stdout_empty(self):
        before = self.lock.read_bytes()
        result = run_cli(self.scene.root, "migrate-lock", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("Lockfile is already normalized", result.stderr)
        self.assertIn("no changes would be written.", result.stderr)
        self.assertEqual(self.lock.read_bytes(), before)
        self.assertEqual(_sidecars(self.scene.root), [])

    def test_a_lock_needing_normalization_prints_json_and_nothing_else(self):
        before = self._denormalize()
        result = run_cli(self.scene.root, "migrate-lock", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertNotIn("generated_at", document)
        self.assertEqual(document["schema"], LOCKFILE_SCHEMA)
        self.assertIn("Would normalize", result.stderr)
        self.assertIn("removed legacy generated_at metadata.", result.stderr)
        self.assertEqual(self.lock.read_bytes(), before)
        self.assertEqual(_sidecars(self.scene.root), [])

    def test_the_previewed_json_is_byte_for_byte_what_a_real_migration_writes(self):
        self._denormalize()
        preview = run_cli(self.scene.root, "migrate-lock", "--dry-run")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        real = run_cli(self.scene.root, "migrate-lock")
        self.assertEqual(real.returncode, 0, real.stderr)
        self.assertEqual(self.lock.read_text(encoding="utf-8"), preview.stdout)

    def test_a_real_migration_does_change_the_bytes(self):
        """The premise: 'unchanged' above is not a migration that does nothing."""
        before = self._denormalize()
        result = run_cli(self.scene.root, "migrate-lock")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.lock.read_bytes(), before)
        self.assertNotIn(
            "generated_at", json.loads(self.lock.read_text(encoding="utf-8"))
        )


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    unittest.main()
