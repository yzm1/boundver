"""Six promises about the distance between a config as written and a config as acted on.

A boundver configuration passes through four narrow places before it changes
anything, and each of the obligations gathered here lives in one of them. A
component name written in the file has to survive a comma-separated transport
shared by `--components`, the GitHub Action input and the GitLab Catalog subset,
or the component is configured and unaddressable at the same time. A YAML
document has to mean what a reviewer reads, which means an anchor, a duplicate
key, a non-string key, an unbounded number or a `<<` merge key must all fail
rather than quietly rewrite the tree that gets fingerprinted. A parse failure
has to say what went wrong without repeating the line it went wrong on, because
these messages are printed into CI logs and pasted into the Action job summary
and a config can hold a credential by accident. And when several gated facets
drift at once the process exit code has to be the maximum severity present,
because pipelines dispatch on it and a compat break downgraded to
"implementation drift" merges.

The two halves of the addressability claim had never met: `service api` is
asserted valid as a config key in test_config_contract.py and is never pushed
through `_parse_components_arg`, so nothing stopped a future tightening of the
splitter from stranding it. The property here builds the config, requires
`validate_config` to accept every generated name, and only then asserts
`_parse_components_arg(name) == [name]` and that joining an accepted subset
splits back to it. Its premise is the mirror image: a name carrying the
delimiter really is torn in two by the splitter, and `validate_config` really
does refuse exactly that name, so the round trip is a live property and not a
tautology about strings without commas.

The confidentiality work needed a premise more than it needed cases. Asserting
that a canary is absent from a message proves nothing unless the message would
have carried it, so every YAML case whose canary sits in a value first makes
PyYAML parse the same text with its own SafeLoader and asserts that PyYAML's
message does echo the canary line; boundver's wrapper is then observably the
thing that removes it. The JSON and TOML branches could not be given that
premise, because CPython's `json` and `tomllib` never quote source. What they
got instead is a test that makes each library raise an exception whose text
carries the canary and shows the wrapper reproducing it verbatim, and an
assertion that every real diagnostic matches a grammar of fixed English and
coordinates, which has nowhere to put source text whatever the canary's
position.

All of that was a claim about a canary sitting in a value, and the first
version of this file swept nothing else while calling itself positional. Moving
the canary to the left of the colon reverses the result, in two branches. A
duplicate mapping key is refused by boundver's own constructors rather than by
the library, and both the YAML and the JSON constructor name the key:
`duplicate YAML mapping key '<key>'` and `duplicate JSON object key '<key>'`.
Stock `yaml.safe_load` and stock `json.loads` both accept a duplicate silently
and keep the last value, so in this position the wrapper is not what removes
the echo, it is what adds it. Nothing truncates below the 500-character
diagnostic cap, a 46-character token is reproduced whole, and `validate-config`
prints the result on stderr, which is the CI log the obligation was written
about. TOML refuses the same document without naming anything and a non-string
YAML key is rendered as its type, so the leak is narrow, and it is pinned that
narrowly.

`validate_config` is the second divergence and the deliberate one. It echoes
unknown field names, provider values, component names and paths through
`_bounded_diagnostic_repr` on purpose, so the obligation's reading is recorded
as an expected failure and every echo it makes is pinned beside it.

Exit-code selection is asserted twice on purpose. At the level of
`_drift_exit_code` a property drives arbitrary configurable component names -
including names that themselves spell `.compat:` - through every permutation of
their issue list, which is the only way to state "independent of iteration
order, component names, and which component carries which facet". At the level
of the process, nine real repositories drift real facets and a real `verify`
subprocess reports a real exit status, including one row where the issue that
selects the code is reported last. Building that fixture took one non-obvious
step: `behavior.paths` must cover every boundary artifact, so a boundary edit
necessarily drifts behavior and exact as well, and only an `impl/` edit isolates
behavior. The slice row needed per-component `verify_facets` so that the compat
severity enters the run through a `SLICE MISMATCH` and not through the component
that carries the version.

Covers OBL-CONFIG-011, OBL-CONFIG-012, OBL-CONFIG-013, OBL-CONFIG-018,
OBL-CONFIG-023 and OBL-PROVIDERS-062.
"""

from __future__ import annotations

import ast
import inspect
import itertools
import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest import mock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from boundver import _config_io
from boundver._config import MAX_CONFIG_BYTES, validate_config
from boundver._config_contract import component_identifier_problem
from boundver._config_io import (
    load_config_file,
    parse_config_bytes,
    parse_config_text,
)
from boundver._git import (
    _ambient_worktree_config_overrides,
    _repository_filter_config_overrides,
)
from boundver._output import _parse_components_arg
from boundver._utils import (
    FACETS,
    MAX_DIAGNOSTIC_VALUE_CHARS,
    MAX_JSON_NUMBER_CHARACTERS,
    ConfigError,
)
from boundver.core import (
    EXIT_BEHAVIOR,
    EXIT_BOUNDARY,
    EXIT_COMPAT,
    EXIT_DRIFT,
    EXIT_OK,
    EXIT_USAGE,
    _drift_exit_code,
)

from tests._parity import run_cli, run_cli_in_process
from tests._scenarios import Scenario

try:  # PyYAML is an optional extra; the hardening obligations need it.
    import yaml
except ImportError:  # pragma: no cover - exercised on hosts without the extra
    yaml = None

requires_yaml = unittest.skipUnless(yaml is not None, "PyYAML is not installed")

PROFILE = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow],
)

SLOW_PROFILE = settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow],
)


# ---------------------------------------------------------------------------
# OBL-CONFIG-011: every configurable name is a selectable name
# ---------------------------------------------------------------------------

#: Characters a component name may carry. The comma is excluded because the
#: transport reserves it; everything else here is either ordinary or a
#: character the splitter's ``strip`` would touch if it appeared at an edge.
NAME_ALPHABET = "abcXYZ019 .:-_/\t"

#: Names chosen because they are exactly what a sampled test leaves out: two
#: with internal whitespace, and three that spell a facet suffix inside the
#: name itself.
AWKWARD_NAMES = (
    "payments api",
    "service api",
    "x.compat: y",
    "y.exact: z",
    "trailing.compat",
)

CONFIGURABLE_NAMES = st.one_of(
    st.text(alphabet=NAME_ALPHABET, min_size=1, max_size=12),
    st.sampled_from(AWKWARD_NAMES),
).filter(lambda value: component_identifier_problem(value) is None)


def _component_config(names) -> Dict[str, Any]:
    """A config whose only interesting content is which names it declares."""
    return {
        "project": "p",
        "components": {
            name: {
                "path": f"c{index}",
                "boundary": {"provider": "leaf", "paths": []},
            }
            for index, name in enumerate(names)
        },
    }


class ComponentNameTransportTests(unittest.TestCase):
    """OBL-CONFIG-011: what validate_config accepts, --components can select."""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._directory.name)
        for index in range(5):
            (cls.root / f"c{index}").mkdir()
            (cls.root / f"c{index}" / "content.txt").write_text("x\n", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    @PROFILE
    @given(st.lists(CONFIGURABLE_NAMES, min_size=1, max_size=5, unique=True))
    def test_every_accepted_name_survives_the_comma_transport(self, names: List[str]):
        config = _component_config(names)
        self.assertEqual(
            validate_config(config, self.root),
            [],
            f"the validator refused a name this property assumed it accepts: {names!r}",
        )
        for name in names:
            self.assertEqual(_parse_components_arg(name), [name])
        joined = ",".join(names)
        self.assertEqual(_parse_components_arg(joined), sorted(set(names)))

    def test_a_name_with_internal_whitespace_is_both_configurable_and_selectable(self):
        """The specific case the register calls out and no test joined up."""
        for name in ("payments api", "service api", "a b  c"):
            with self.subTest(name=name):
                self.assertIsNone(component_identifier_problem(name))
                self.assertEqual(validate_config(_component_config([name]), self.root), [])
                self.assertEqual(_parse_components_arg(name), [name])

    def test_the_splitter_would_have_shown_a_name_it_could_not_carry(self):
        """The premise: the transport really does tear a name on its delimiter."""
        self.assertEqual(_parse_components_arg("a,b"), ["a", "b"])
        self.assertEqual(_parse_components_arg(" padded "), ["padded"])
        self.assertEqual(
            component_identifier_problem("a,b"),
            "must not contain ',' because CLI, GitHub Action, and GitLab "
            "component filters are comma-separated",
        )
        self.assertEqual(
            component_identifier_problem(" padded "),
            "must not have leading or trailing whitespace",
        )
        errors = validate_config(_component_config(["a,b"]), self.root)
        self.assertTrue(
            any("is not addressable" in error for error in errors),
            errors,
        )


# ---------------------------------------------------------------------------
# OBL-CONFIG-012: YAML fails closed
# ---------------------------------------------------------------------------

#: A merge key whose value is an inline mapping, so no alias is involved and
#: the merge machinery itself is what is being refused.
INLINE_MERGE_YAML = (
    "project: p\n"
    "components:\n"
    "  svc:\n"
    "    <<:\n"
    "      boundary:\n"
    "        provider: leaf\n"
    "    path: svc\n"
)

#: The same injection written the way a human writes it, with an anchor.
ANCHORED_MERGE_YAML = (
    "base: &base\n"
    "  boundary:\n"
    "    provider: leaf\n"
    "project: p\n"
    "components:\n"
    "  svc:\n"
    "    <<: *base\n"
    "    path: svc\n"
)

#: Each hostile YAML construct and the exact diagnostic boundver produces for
#: it. The path is spelled the way it reaches the parser, so the coordinates
#: in the two positional messages belong to the text in this same table.
REJECTED_YAML = {
    "an alias reference": (
        "project: &p name\nother: *p\ncomponents: {}\n",
        "YAML parse error in boundary.config.yaml: "
        "YAML aliases are not supported in boundver config",
    ),
    "a merge key fed by an anchor": (
        ANCHORED_MERGE_YAML,
        "YAML parse error in boundary.config.yaml: "
        "YAML aliases are not supported in boundver config",
    ),
    "a merge key fed by an inline mapping": (
        INLINE_MERGE_YAML,
        "YAML parse error in boundary.config.yaml: ConstructorError at line 4, column 5",
    ),
    "a duplicate mapping key": (
        "project: a\nproject: b\ncomponents: {}\n",
        "YAML parse error in boundary.config.yaml: "
        "duplicate YAML mapping key at line 2, column 1",
    ),
    "a non-string mapping key": (
        "1: a\ncomponents: {}\n",
        "YAML parse error in boundary.config.yaml: "
        "YAML mapping keys must be strings at line 1, column 1",
    ),
    "an integer over the numeric character limit": (
        "project: p\nn: " + "1" * (MAX_JSON_NUMBER_CHARACTERS + 5) + "\n",
        "YAML parse error in boundary.config.yaml: invalid YAML integer: "
        "YAML integer representation exceeds the safety limit",
    ),
    "a float over the numeric character limit": (
        "project: p\nn: 1." + "0" * (MAX_JSON_NUMBER_CHARACTERS + 5) + "\n",
        "YAML parse error in boundary.config.yaml: "
        f"YAML number exceeds the {MAX_JSON_NUMBER_CHARACTERS}-character limit",
    ),
}

YAML_PATH = Path("boundary.config.yaml")


@requires_yaml
class YamlHardeningTests(unittest.TestCase):
    """OBL-CONFIG-012: six ways to make a config differ from what a reviewer reads."""

    def test_each_hostile_construct_is_refused_with_its_own_diagnostic(self):
        for label, (text, expected) in REJECTED_YAML.items():
            with self.subTest(construct=label):
                with self.assertRaises(ConfigError) as caught:
                    parse_config_text(text, YAML_PATH)
                self.assertEqual(str(caught.exception), expected)

    def test_the_ordinary_forms_of_each_refusal_still_parse(self):
        """The premise: every constructor above is installed and reached.

        Without this, each rejection could be a loader that refuses YAML
        wholesale rather than one that refuses these six constructs.
        """
        parsed = parse_config_text(
            "project: p\nn: 12\nf: 1.5\nnested:\n  a: b\n", YAML_PATH
        )
        self.assertEqual(
            parsed, {"project": "p", "n": 12, "f": 1.5, "nested": {"a": "b"}}
        )

    def test_stock_pyyaml_would_have_injected_the_block_the_merge_key_hides(self):
        """The premise for the merge refusal, and the reason it matters.

        PyYAML's own SafeLoader flattens both spellings and produces a `svc`
        declaration carrying a `boundary` block that appears nowhere under
        `svc` in the document. That is the injection the obligation is about,
        so this test states it as an observation rather than as a worry.
        """
        for label, text in (
            ("inline mapping", INLINE_MERGE_YAML),
            ("anchor", ANCHORED_MERGE_YAML),
        ):
            with self.subTest(spelling=label):
                flattened = yaml.safe_load(text)
                self.assertEqual(
                    flattened["components"]["svc"],
                    {"boundary": {"provider": "leaf"}, "path": "svc"},
                )
                self.assertNotIn("<<", flattened["components"]["svc"])

    def test_a_merge_key_is_refused_at_parse_time_rather_than_surfaced_as_a_field(self):
        """The register expected a literal '<<' field; the loader is stricter.

        `construct_mapping` never calls `flatten_mapping`, but the merge key
        never reaches `construct_mapping` either: PyYAML's resolver tags `<<`
        as `tag:yaml.org,2002:merge`, SafeConstructor registers no constructor
        for that tag, and `construct_undefined` raises. So the document is
        rejected outright, which is stronger than surfacing '<<' as an unknown
        field. Both halves are pinned here so a change in either direction is
        visible.
        """
        with self.assertRaises(ConfigError) as caught:
            parse_config_text(INLINE_MERGE_YAML, YAML_PATH)
        message = str(caught.exception)
        self.assertEqual(
            message,
            "YAML parse error in boundary.config.yaml: ConstructorError at line 4, column 5",
        )
        self.assertNotIn("<<", message)

    def test_a_mapping_that_merely_looks_like_a_merge_is_kept_as_a_literal_key(self):
        """A quoted '<<' is data, not a directive, and stays a field.

        This separates the two possible reasons the document above fails: the
        merge *tag*, not the two characters.
        """
        parsed = parse_config_text('project: p\n"<<x": 1\n', YAML_PATH)
        self.assertEqual(parsed, {"project": "p", "<<x": 1})


# ---------------------------------------------------------------------------
# OBL-CONFIG-013: a diagnostic never repeats the config
# ---------------------------------------------------------------------------

#: Malformed configs by format, by where the canary sits relative to the
#: failure, and by whether the underlying library's own message repeats it -
#: which is what decides whether a case can carry its own premise. ``position``
#: is load-bearing, not a label: the grid these rows and KEY_POSITION_DIAGNOSTICS
#: cover between them is asserted below, because the first version of this table
#: put every canary in a value and still called itself a positional sweep.
MALFORMED_TEMPLATES = {
    "json canary in a value on the failing line": (
        ".json",
        '{"project": "%s" "components": {}}\n',
        "value on the failing line",
        False,
    ),
    "json canary in a key before a missing comma": (
        ".json",
        '{\n  "%s": 1\n  "components": {}\n}\n',
        "key",
        False,
    ),
    "json canary on the line adjacent to the error": (
        ".json",
        '{\n  "project": "%s",\n  "components": {,}\n}\n',
        "value on a neighbouring line",
        False,
    ),
    "yaml canary in a value on the failing line": (
        ".yaml",
        "project: secret %s: oops\n",
        "value on the failing line",
        True,
    ),
    "yaml canary inside an unterminated quote": (
        ".yaml",
        'project: "%s\n',
        "value on the failing line",
        True,
    ),
    "yaml canary inside an unterminated flow mapping": (
        ".yaml",
        "project: {a: %s\n",
        "value on the failing line",
        True,
    ),
    "yaml canary a line above an unterminated flow mapping": (
        ".yaml",
        'project: "%s"\ncomponents: {\n',
        "value on a neighbouring line",
        False,
    ),
    "toml canary in a value on the failing line": (
        ".toml",
        'project = "%s" oops\n',
        "value on the failing line",
        False,
    ),
    "toml canary in a value before a bad table header": (
        ".toml",
        'project = "%s"\n[[[bad\n',
        "value on a neighbouring line",
        False,
    ),
    "toml canary in a key before a bad assignment": (
        ".toml",
        "%s = = 1\n",
        "key",
        False,
    ),
}

#: The whole grammar of a parse diagnostic, after the ``FORMAT parse error in
#: PATH: `` prefix: a fixed English phrase and coordinates, with nowhere to put
#: a byte of the document. Observed against every row above at three canary
#: lengths. This is the assertion that makes the ``leaks_raw=False`` rows worth
#: their budget - "the canary is absent" is a statement about one string,
#: "the message is only ever a phrase and two numbers" is a statement about all
#: of them.
DIAGNOSTIC_TAILS = {
    ".json": re.compile(r"^[A-Za-z]+Error at line \d+, column \d+$"),
    ".yaml": re.compile(r"^[A-Za-z]+Error at line \d+, column \d+$"),
    ".toml": re.compile(r"^[A-Za-z]+Error at line \d+, column \d+$"),
}

FORMAT_LABEL = {".json": "JSON", ".yaml": "YAML", ".toml": "TOML"}

#: Documents whose diagnostic quotes a KEY, with the exact text observed for a
#: 16-character canary. The first three name the key; the last two are the
#: contrast, refusing a document of the same shape without naming anything.
#: The TOML column is the canary's length plus five: `NAME = 1` is one past
#: `1`, and it moves with the canary, which is why it is computed here.
KEY_POSITION_DIAGNOSTICS = {
    "a duplicate key at the root of a YAML document": (
        ".yaml",
        lambda canary: f"{canary}: a\n{canary}: b\ncomponents: {{}}\n",
        lambda canary, path: (
            f"YAML parse error in {path}: duplicate YAML mapping key "
            "at line 2, column 1"
        ),
        False,
    ),
    "a duplicate key nested inside a YAML component": (
        ".yaml",
        lambda canary: (
            f"project: p\ncomponents:\n  svc:\n    {canary}: 1\n    {canary}: 2\n"
        ),
        lambda canary, path: (
            f"YAML parse error in {path}: duplicate YAML mapping key "
            "at line 5, column 5"
        ),
        False,
    ),
    "a duplicate key in a JSON object": (
        ".json",
        lambda canary: f'{{"{canary}": 1, "{canary}": 2}}\n',
        lambda canary, path: f"JSON parse error in {path}: duplicate JSON object key",
        False,
    ),
    "a duplicate key in a TOML table": (
        ".toml",
        lambda canary: f"{canary} = 1\n{canary} = 2\n",
        lambda canary, path: (
            f"TOML parse error in {path}: TOMLDecodeError "
            f"at line 2, column {len(canary) + 5}"
        ),
        False,
    ),
    "a non-string YAML key beside a canary value": (
        ".yaml",
        lambda canary: f"1: {canary}\ncomponents: {{}}\n",
        lambda canary, path: (
            f"YAML parse error in {path}: YAML mapping keys must be strings "
            "at line 1, column 1"
        ),
        False,
    ),
}

#: Canaries are drawn from an alphabet no diagnostic can produce by accident,
#: and carry a fixed marker so a coincidental substring is impossible.
CANARIES = st.text(
    alphabet="ABCDEFGHJKLMNPQRSTUVWXYZ0123456789", min_size=8, max_size=16
).map(lambda tail: "ZQXCANARY" + tail)

#: The validate_config diagnostics that repeat config content on purpose,
#: each with the config that produces it and the exact text observed.
ECHOING_DIAGNOSTICS = {
    "an unknown root field is named": (
        lambda canary: {"project": "p", canary: 1, "components": {}},
        lambda canary: f"Unknown field in config: {canary}",
    ),
    "an unsupported provider is quoted": (
        lambda canary: {
            "project": "p",
            "components": {
                "svc": {"path": "svc", "boundary": {"provider": canary, "paths": []}}
            },
        },
        lambda canary: (
            f"Component 'svc' has unsupported boundary.provider '{canary}' "
            "(use a known provider or custom.* namespace)"
        ),
    ),
    "a missing component path is echoed": (
        lambda canary: {
            "project": "p",
            "components": {
                "svc": {"path": canary, "boundary": {"provider": "leaf", "paths": []}}
            },
        },
        lambda canary: f"Component 'svc' path not found or not a directory: {canary}",
    ),
}


class ConfigDiagnosticConfidentialityTests(unittest.TestCase):
    """OBL-CONFIG-013: parse failures name the failure, never the file."""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._directory.name)
        (cls.root / "svc").mkdir()
        (cls.root / "svc" / "content.txt").write_text("x\n", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def _config_path(self, suffix: str) -> Path:
        return self.root / ("boundary.config" + suffix)

    def _messages(self, suffix: str, text: str) -> Dict[str, str]:
        """Every entry point's diagnostic for one malformed document."""
        path = self._config_path(suffix)
        path.write_text(text, encoding="utf-8")
        messages: Dict[str, str] = {}
        for entry, call in (
            ("parse_config_text", lambda: parse_config_text(text, path)),
            (
                "parse_config_bytes",
                lambda: parse_config_bytes(
                    text.encode("utf-8"), path, max_bytes=MAX_CONFIG_BYTES
                ),
            ),
            (
                "load_config_file",
                lambda: load_config_file(path, max_bytes=MAX_CONFIG_BYTES),
            ),
        ):
            with self.assertRaises(ConfigError, msg=f"{entry} accepted the document"):
                try:
                    call()
                except ConfigError as exc:
                    messages[entry] = str(exc)
                    raise
        return messages

    @PROFILE
    @given(CANARIES, st.sampled_from(sorted(MALFORMED_TEMPLATES)))
    def test_no_parse_diagnostic_repeats_a_line_of_the_config(self, canary, label):
        suffix, template, _position, leaks_raw = MALFORMED_TEMPLATES[label]
        if suffix in (".yaml", ".yml") and yaml is None:  # pragma: no cover
            self.skipTest("PyYAML is not installed")
        text = template % canary
        if leaks_raw:
            # The premise, re-established for every generated canary: the
            # library boundver wraps does repeat this line.
            with self.assertRaises(yaml.YAMLError) as caught:
                yaml.safe_load(text)
            self.assertIn(canary, str(caught.exception))
        prefix = f"{FORMAT_LABEL[suffix]} parse error in {self._config_path(suffix)}: "
        for entry, message in self._messages(suffix, text).items():
            self.assertNotIn(canary, message, f"{entry} leaked the canary: {message}")
            # Stronger than the absence above, and the reason a row with no
            # premise still earns its place: whatever the canary's position,
            # the message is a fixed phrase and coordinates and nothing else.
            self.assertTrue(message.startswith(prefix), f"{entry}: {message}")
            self.assertRegex(message[len(prefix) :], DIAGNOSTIC_TAILS[suffix])

    def test_the_confidentiality_sweep_covers_every_format_and_position(self):
        """The tables have to perform the sweep their labels claim.

        Nine cells: three config formats crossed with the three places the
        obligation names - a canary in a value on the failing line, in a value
        on the line next to it, and in a key. The first version of this file
        filled only the value cells and reported the obligation covered. Two of
        the three key cells turn out to leak, so they live in the divergence
        table below rather than in the property above, and this assertion is
        what stops that split from quietly losing one.
        """
        grid = {
            (suffix, position)
            for suffix in (".json", ".yaml", ".toml")
            for position in (
                "key",
                "value on the failing line",
                "value on a neighbouring line",
            )
        }
        self.assertEqual(
            {
                (suffix, position)
                for suffix, _template, position, _leaks in MALFORMED_TEMPLATES.values()
            },
            grid - {(".yaml", "key")},
            "the no-leak property lost a cell it used to sweep",
        )
        # The one missing cell is missing for a reason, not by omission. A YAML
        # key that is well-formed and unique parses, so the only YAML documents
        # that fail with a canary in key position are the duplicate ones - and
        # those leak. The cell is swept by the divergence table instead, which
        # has to reach all three formats so the leak stays pinned as specific
        # to two of them.
        self.assertEqual(
            {
                suffix
                for suffix, _build, _expected, _leaks in KEY_POSITION_DIAGNOSTICS.values()
            },
            {".json", ".yaml", ".toml"},
        )

    def test_pyyaml_would_have_printed_the_canary_line_verbatim(self):
        """The premise, stated once concretely with the text it produces."""
        if yaml is None:  # pragma: no cover
            self.skipTest("PyYAML is not installed")
        text = "project: secret ZQXCANARY7788: oops\n"
        with self.assertRaises(yaml.YAMLError) as caught:
            yaml.safe_load(text)
        self.assertIn("project: secret ZQXCANARY7788: oops", str(caught.exception))
        with self.assertRaises(ConfigError) as wrapped:
            parse_config_text(text, YAML_PATH)
        self.assertEqual(
            str(wrapped.exception),
            "YAML parse error in boundary.config.yaml: ScannerError at line 1, column 30",
        )

    def test_json_and_toml_parser_exceptions_do_not_reach_diagnostics(self):
        """Parser-library text is untrusted because it can quote source input."""
        canary = "ZQXCANARY7788"

        def exploding_json(_text):
            raise ValueError(f"parse failed near {canary}")

        with mock.patch.object(_config_io, "strict_json_loads", exploding_json):
            with self.assertRaises(ConfigError) as caught:
                parse_config_text("{}", Path("boundary.config.json"))
        self.assertEqual(
            str(caught.exception),
            "JSON parse error in boundary.config.json: ValueError",
        )

        with mock.patch("tomllib.loads", side_effect=ValueError(f"bad token {canary}")):
            with self.assertRaises(ConfigError) as caught:
                parse_config_text('project = "p"\n', Path("boundary.config.toml"))
        self.assertEqual(
            str(caught.exception),
            "TOML parse error in boundary.config.toml: ValueError",
        )
        self.assertNotIn(canary, str(caught.exception))

    def test_a_duplicate_key_is_named_by_the_yaml_and_json_diagnostics(self):
        """Pins the current text of all five key-position diagnostics.

        Three of the five repeat the key - two YAML documents and one JSON one,
        so two branches - and two do not. Every message is asserted as a whole
        string, so a partial fix, stripping the YAML echo and leaving the JSON
        one or bounding either, fails here rather than passing as an
        improvement.

        The first assertion names which two leak. Without it, deleting the two
        leaking rows would empty this test and turn the expected failure below
        into an unexpected success for a reason nobody wrote down; with it, the
        divergence cannot be retired by deletion, only by a real fix.
        """
        self.assertEqual(
            {label for label, row in KEY_POSITION_DIAGNOSTICS.items() if row[3]},
            set(),
        )
        canary = "ZQXCANARY7788AAA"
        for label, (
            suffix,
            build,
            expected,
            leaks,
        ) in KEY_POSITION_DIAGNOSTICS.items():
            if suffix in (".yaml", ".yml") and yaml is None:  # pragma: no cover
                continue
            with self.subTest(document=label):
                path = self._config_path(suffix)
                for entry, message in self._messages(suffix, build(canary)).items():
                    self.assertEqual(message, expected(canary, path), entry)
                    self.assertEqual(canary in message, leaks, f"{entry}: {message}")

    @requires_yaml
    def test_neither_library_refuses_a_duplicate_key_on_its_own(self):
        """The premise that makes the echo boundver's, not the library's.

        This is the mirror image of the premise the value-position rows carry.
        There, PyYAML repeats the source line and boundver's wrapper is what
        removes it. Here, both stock loaders accept the document silently and
        keep the last value, so the diagnostic that names the key exists only
        because boundver's own `construct_mapping` and `strict_json_loads`
        chose to raise and to interpolate the key into the reason.
        """
        canary = "ZQXCANARY7788AAA"
        self.assertEqual(
            yaml.safe_load(f"{canary}: a\n{canary}: b\n"), {canary: "b"}
        )
        self.assertEqual(
            json.loads(f'{{"{canary}": 1, "{canary}": 2}}'), {canary: 2}
        )

    def test_a_credential_sized_duplicate_key_is_not_echoed(self):
        """A credential-shaped mapping key must not reach any diagnostic."""
        token = "ghp_" + "0123456789abcdef" * 2 + "0123456789"
        self.assertEqual(len(token), 46)
        self.assertLess(len(token), MAX_DIAGNOSTIC_VALUE_CHARS)
        for label, (suffix, build, expected, _leaks) in KEY_POSITION_DIAGNOSTICS.items():
            if suffix in (".yaml", ".yml") and yaml is None:  # pragma: no cover
                continue
            with self.subTest(document=label):
                path = self._config_path(suffix)
                for entry, message in self._messages(suffix, build(token)).items():
                    self.assertNotIn(token, message, entry)
                    self.assertEqual(message, expected(token, path), entry)

    @requires_yaml
    def test_a_duplicate_key_does_not_reach_validate_config_output(self):
        """The CLI must preserve the parser's content-free diagnostic boundary."""
        canary = "ZQXCANARY7788AAA"
        documents = {
            "canary in a duplicate key": (
                f"project: p\n{canary}: a\n{canary}: b\ncomponents: {{}}\n",
                False,
            ),
            "canary in a value on the failing line": (
                f"project: secret {canary}: oops\n",
                False,
            ),
        }
        for label, (text, leaks) in documents.items():
            with self.subTest(document=label):
                with Scenario("p") as scene:
                    scene.commit()
                    (scene.root / "boundary.config.json").unlink()
                    (scene.root / "boundary.config.yaml").write_text(
                        text, encoding="utf-8"
                    )
                    result = run_cli(scene.root, "validate-config")
                self.assertEqual(result.returncode, EXIT_USAGE, result.stderr)
                printed = result.stdout + result.stderr
                self.assertEqual(canary in printed, leaks, printed)
                self.assertNotIn(canary, printed)

    def test_no_parse_diagnostic_repeats_a_key_of_the_config(self):
        """Duplicate-key diagnostics identify structure without quoting keys."""
        canary = "ZQXCANARY7788AAA"
        for suffix, build, _expected, _leaks in KEY_POSITION_DIAGNOSTICS.values():
            if suffix in (".yaml", ".yml") and yaml is None:  # pragma: no cover
                continue
            for message in self._messages(suffix, build(canary)).values():
                self.assertNotIn(canary, message)

    def test_validate_config_echoes_config_content_on_purpose(self):
        """Pins each deliberate echo, so a partial change cannot pass unseen."""
        canary = "ZQXCANARY7788"
        for label, (build, expected) in ECHOING_DIAGNOSTICS.items():
            with self.subTest(diagnostic=label):
                errors = validate_config(build(canary), self.root)
                self.assertIn(expected(canary), errors, errors)

    def test_validate_config_echoes_an_unknown_source_mode(self):
        self.assertEqual(
            validate_config(
                {"project": "p", "components": {}}, self.root, source="ZQXCANARY7788"
            ),
            ["Unknown source mode: 'ZQXCANARY7788'"],
        )


# ---------------------------------------------------------------------------
# OBL-CONFIG-018: the selection rule, over arbitrary names and orders
# ---------------------------------------------------------------------------

#: The exit code each gated facet contributes. This is the oracle: a table and
#: a max, written independently of the if-chain in `_drift_exit_code`.
FACET_SEVERITY = {
    "exact": EXIT_DRIFT,
    "behavior": EXIT_BEHAVIOR,
    "boundary": EXIT_BOUNDARY,
    "compat": EXIT_COMPAT,
}

ISSUE_KINDS = ("MISMATCH", "SLICE MISMATCH")


def _issue(kind: str, name: str, facet: str) -> str:
    """One drift issue in the shape `_lockfile` emits."""
    return f"{kind} {name}.{facet}: lockfile=aaaabbbbcccc... current=ddddeeeeffff..."


DRIFT_ISSUES = st.tuples(
    st.sampled_from(ISSUE_KINDS),
    CONFIGURABLE_NAMES,
    st.sampled_from(sorted(FACET_SEVERITY)),
)


def _product_safety_prefixes() -> Tuple[str, ...]:
    """The prefixes that make an issue a usage error, read from the product.

    Copying the tuple into this file would make it a mirror that never
    disagrees: a thirteenth prefix added to `_drift_exit_code` would go
    unexercised, exactly the way a fifth facet would go unsevered. It is a
    local, so it cannot be imported; lifting the literal out of the function's
    own source is the next best thing, and it fails loudly if the shape ever
    changes rather than silently testing a stale list.
    """
    source = inspect.getsource(_drift_exit_code)
    match = re.search(r"safety_prefixes\s*=\s*(\([^)]*\))", source, re.S)
    if match is None:  # pragma: no cover - the shape changed, say so
        raise AssertionError("could not find safety_prefixes in _drift_exit_code")
    return ast.literal_eval(match.group(1))


class DriftExitCodeSelectionTests(unittest.TestCase):
    """OBL-CONFIG-018: the maximum, not the first, the last, or the nearest."""

    @PROFILE
    @given(st.lists(DRIFT_ISSUES, min_size=1, max_size=4))
    def test_the_code_is_the_maximum_severity_under_every_ordering(self, rows):
        issues = [_issue(kind, name, facet) for kind, name, facet in rows]
        expected = max(FACET_SEVERITY[facet] for _kind, _name, facet in rows)
        for ordering in itertools.permutations(range(len(issues))):
            permuted = [issues[index] for index in ordering]
            self.assertEqual(
                _drift_exit_code(permuted),
                expected,
                f"ordering {ordering} changed the code for {rows!r}",
            )

    @PROFILE
    @given(st.lists(DRIFT_ISSUES, min_size=1, max_size=4))
    def test_the_code_does_not_depend_on_what_the_components_are_called(self, rows):
        as_written = [_issue(kind, name, facet) for kind, name, facet in rows]
        renamed = [
            _issue(kind, f"c{index}", facet)
            for index, (kind, _name, facet) in enumerate(rows)
        ]
        self.assertEqual(_drift_exit_code(as_written), _drift_exit_code(renamed))

    def test_each_facet_alone_selects_a_distinct_code(self):
        """The premise: the four severities are reachable and different.

        An invariance property over a function that answered 1 for everything
        would pass; this is what stops that reading. The first assertion is a
        second guard, on the oracle rather than on the code: FACET_SEVERITY is
        a hand-written mirror of a product surface, and a fifth gated facet
        added to `boundver._utils.FACETS` would otherwise fall through
        `_drift_exit_code`'s if-chain to EXIT_DRIFT with nothing here noticing.
        Adding a facet has to force a decision about its severity.
        """
        self.assertEqual(
            set(FACET_SEVERITY),
            set(FACETS),
            "the product's facet surface moved; give the new facet a severity "
            "in FACET_SEVERITY rather than letting it default to EXIT_DRIFT",
        )
        observed = {
            facet: _drift_exit_code([_issue("MISMATCH", "svc", facet)])
            for facet in FACET_SEVERITY
        }
        self.assertEqual(observed, FACET_SEVERITY)
        self.assertEqual(len(set(observed.values())), 4)

    def test_a_name_that_spells_a_facet_suffix_does_not_change_the_answer(self):
        """The names in AWKWARD_NAMES are configurable, so this is reachable."""
        for name in AWKWARD_NAMES:
            with self.subTest(name=name):
                self.assertIsNone(component_identifier_problem(name))
                for facet, code in FACET_SEVERITY.items():
                    self.assertEqual(
                        _drift_exit_code([_issue("MISMATCH", name, facet)]), code
                    )

    def test_an_unavailable_facet_is_a_usage_error_not_the_severity_it_names(self):
        """`UNAVAILABLE FACET` matches the facet regex but is not drift."""
        self.assertEqual(
            _drift_exit_code(
                [
                    "UNAVAILABLE FACET svc.compat: selected gate requires both "
                    "locked and current digests"
                ]
            ),
            EXIT_USAGE,
        )
        self.assertEqual(
            _drift_exit_code(
                [
                    _issue("MISMATCH", "svc", "boundary"),
                    "UNAVAILABLE FACET other.compat: selected gate requires both "
                    "locked and current digests",
                ]
            ),
            EXIT_USAGE,
        )

    def test_every_safety_prefix_outranks_the_highest_drift_severity(self):
        """All safety prefixes, in both positions, against the code they suppress.

        The test above covers the one prefix that also matches the facet regex.
        The other prefixes were exercised by nothing, so one dropped from the
        tuple would have let a run that could not complete report a drift code
        a pipeline treats as a real answer. The premise is the first assertion:
        the same compat drift on its own really does select EXIT_COMPAT, so
        each row below is observing a suppression rather than a coincidence.
        """
        prefixes = _product_safety_prefixes()
        self.assertEqual(len(prefixes), 17, prefixes)
        drift = _issue("MISMATCH", "svc", "compat")
        self.assertEqual(_drift_exit_code([drift]), EXIT_COMPAT)
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                issue = f"{prefix}: a diagnostic the run could not complete"
                self.assertEqual(_drift_exit_code([issue]), EXIT_USAGE)
                self.assertEqual(_drift_exit_code([drift, issue]), EXIT_USAGE)
                self.assertEqual(_drift_exit_code([issue, drift]), EXIT_USAGE)

    def test_a_metadata_mismatch_carries_no_facet_severity(self):
        """Observed alongside every compat drift, and it must not raise the code."""
        self.assertEqual(
            _drift_exit_code(
                ["METADATA MISMATCH svc.version: lockfile='1.0.0' current='2.0.0'"]
            ),
            EXIT_DRIFT,
        )


# ---------------------------------------------------------------------------
# OBL-CONFIG-023: the same rule, observed as a process exit status
# ---------------------------------------------------------------------------

#: The component tree every exit-code repository starts from. `behavior` must
#: cover every boundary artifact, so a boundary edit necessarily drifts
#: behavior and exact too; only the `impl/` file isolates behavior.
BASELINE_FILES = {
    "api/contract.yaml": "openapi: 3.1.0\n",
    "impl/service.py": "def go():\n    return 1\n",
    "version.json": '{"version": "1.0.0"}\n',
    "notes.txt": "one\n",
}

#: The single edit that drifts each facet, and nothing more severe.
FACET_EDIT = {
    "exact": ("notes.txt", "two\n"),
    "behavior": ("impl/service.py", "def go():\n    return 2\n"),
    "boundary": ("api/contract.yaml", "openapi: 3.1.0\npaths: {}\n"),
    "compat": ("version.json", '{"version": "2.0.0"}\n'),
}

#: Which components drift which facets, and the exit code the maximum rule
#: requires. The four single-facet rows are the premise for the rest.
EXIT_MATRIX = {
    "exact alone": ({"alpha": ("exact",)}, EXIT_DRIFT),
    "behavior alone": ({"alpha": ("behavior",)}, EXIT_BEHAVIOR),
    "boundary alone": ({"alpha": ("boundary",)}, EXIT_BOUNDARY),
    "compat alone": ({"alpha": ("compat",)}, EXIT_COMPAT),
    "exact in alpha, behavior in beta": (
        {"alpha": ("exact",), "beta": ("behavior",)},
        EXIT_BEHAVIOR,
    ),
    "behavior in alpha, boundary in beta": (
        {"alpha": ("behavior",), "beta": ("boundary",)},
        EXIT_BOUNDARY,
    ),
    "boundary in alpha, compat in beta": (
        {"alpha": ("boundary",), "beta": ("compat",)},
        EXIT_COMPAT,
    ),
    "behavior in alpha, compat in beta": (
        {"alpha": ("behavior",), "beta": ("compat",)},
        EXIT_COMPAT,
    ),
    "compat in alpha, boundary in beta": (
        {"alpha": ("compat",), "beta": ("boundary",)},
        EXIT_COMPAT,
    ),
}


def _populate(scene: Scenario, name: str) -> None:
    for relative, content in BASELINE_FILES.items():
        scene.file(f"{name}/{relative}", content)


def _drift_repository(names, *, verify_facets=None, slices=None) -> Scenario:
    """A committed repository with a generated lockfile, ready to drift."""
    scene = Scenario("p")
    scene.defaults(compat_mode="major")
    for name in names:
        scene.component(
            name,
            path=name,
            provider="path-hash",
            boundary=["api/*.yaml"],
            behavior=["api/*.yaml", "impl/*.py"],
            version_source={"file": "version.json", "field": "version"},
            verify_facets=(verify_facets or {}).get(name),
        )
        _populate(scene, name)
    for slice_name, definition in (slices or {}).items():
        scene.slice(slice_name, **definition)
    scene.commit()
    generated = run_cli(scene.root, "generate", "--source", "working-tree")
    if generated.returncode != EXIT_OK:
        scene.close()
        raise AssertionError(f"fixture generate failed: {generated.stderr}")
    return scene


def _apply(scene: Scenario, plan: Dict[str, Tuple[str, ...]]) -> None:
    for component, facets in plan.items():
        for facet in facets:
            relative, content = FACET_EDIT[facet]
            scene.file(f"{component}/{relative}", content)


def _verify(scene: Scenario) -> Tuple[int, List[str]]:
    result = run_cli(
        scene.root, "verify", "--source", "working-tree", "--format", "json"
    )
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.returncode, payload.get("issues", [])


class VerifyProcessExitCodeTests(unittest.TestCase):
    """OBL-CONFIG-023: the status a pipeline dispatches on."""

    NAMES = ("alpha", "beta")

    @classmethod
    def setUpClass(cls):
        cls.scene = _drift_repository(cls.NAMES)

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()

    def setUp(self):
        for name in self.NAMES:
            _populate(self.scene, name)

    def test_a_clean_tree_verifies_before_anything_is_drifted(self):
        """The premise for every row: the fixture is not failing already."""
        code, issues = _verify(self.scene)
        self.assertEqual(code, EXIT_OK, issues)
        self.assertEqual(issues, [])

    def test_the_exit_code_is_the_highest_severity_facet_that_drifted(self):
        for label, (plan, expected) in EXIT_MATRIX.items():
            with self.subTest(case=label):
                for name in self.NAMES:
                    _populate(self.scene, name)
                _apply(self.scene, plan)
                code, issues = _verify(self.scene)
                self.assertEqual(code, expected, issues)

    def test_the_issue_that_selects_the_code_is_not_always_the_first_reported(self):
        """A first-encountered implementation would answer 1 here, not 3."""
        _apply(self.scene, {"alpha": ("exact",), "beta": ("behavior",)})
        code, issues = _verify(self.scene)
        self.assertEqual(code, EXIT_BEHAVIOR, issues)
        self.assertTrue(issues[0].startswith("MISMATCH alpha.exact: "), issues)
        self.assertTrue(
            any(issue.startswith("MISMATCH beta.behavior: ") for issue in issues),
            issues,
        )

    def test_permuting_which_component_carries_which_facet_holds_the_code(self):
        """The metamorphic half, run against real repositories.

        Four arrangements of the same two drifts: both orders of declaration,
        and both assignments of facet to component. Both axes are asserted to
        have moved, separately. All four issue lists differ, which the carrier
        swap alone would have satisfied; so the second loop isolates the axis
        the carrier swap cannot reach. Holding the carrier fixed and reversing
        only the declaration order leaves the set of issues identical and their
        sequence different, which is the insertion-order clause of the
        obligation stated as an observation rather than assumed.
        """
        observed = {}
        for order in (("alpha", "beta"), ("beta", "alpha")):
            for carrier in order:
                other = order[1] if carrier == order[0] else order[0]
                with self.subTest(order=order, boundary_on=carrier):
                    with _drift_repository(order) as scene:
                        _apply(scene, {carrier: ("boundary",), other: ("behavior",)})
                        code, issues = _verify(scene)
                        self.assertEqual(code, EXIT_BOUNDARY, issues)
                        observed[(order, carrier)] = tuple(issues)
        self.assertEqual(len(observed), 4)
        self.assertEqual(
            len(set(observed.values())),
            4,
            "two arrangements produced the same issue list, so at least one of "
            "the axes this test claims to permute is inert",
        )
        for carrier in self.NAMES:
            with self.subTest(declaration_order_only=carrier):
                first = observed[(("alpha", "beta"), carrier)]
                second = observed[(("beta", "alpha"), carrier)]
                self.assertEqual(sorted(first), sorted(second))
                self.assertNotEqual(first, second)

    def test_a_slice_mismatch_raises_the_code_above_every_component_issue(self):
        """Compat enters through the slice, and it is the last issue reported.

        `alpha` is gated on exact only, so its own compat drift is not gating.
        `beta` is gated on compat, which is what makes the slice's compat mode
        a gate. `gamma` supplies a component boundary mismatch. Both a
        first-encountered and a per-component implementation answer 4 here.
        """
        with _drift_repository(
            ("alpha", "beta", "gamma"),
            verify_facets={"alpha": ["exact"], "beta": ["compat"], "gamma": ["boundary"]},
            slices={"release": {"mode": "compat", "components": ["alpha", "beta"]}},
        ) as scene:
            _apply(scene, {"alpha": ("compat",), "gamma": ("boundary",)})
            code, issues = _verify(scene)
            self.assertEqual(code, EXIT_COMPAT, issues)
            self.assertTrue(
                any(issue.startswith("MISMATCH gamma.boundary: ") for issue in issues),
                issues,
            )
            self.assertTrue(
                issues[-1].startswith("SLICE MISMATCH release.compat: "), issues
            )
            self.assertFalse(
                any(issue.startswith("MISMATCH alpha.compat: ") for issue in issues),
                "alpha's own compat drift was gated, so the slice is no longer "
                "the only carrier of compat severity",
            )


# ---------------------------------------------------------------------------
# OBL-PROVIDERS-062: what `add` and `remove` write back
# ---------------------------------------------------------------------------

#: A config carrying one of every root field and every component field the
#: mutation commands do not touch, plus two literal non-ASCII characters.
#: Everything here must come back unchanged in value, and `café`/`naïve` must
#: come back escaped. `providers` is in the list because OBL-PROVIDERS-062
#: names it and because it is the one carried-through field whose loss would
#: change which provider classes load rather than only how the file reads.
#: `add` validates but does not import, so an unreferenced declaration is
#: accepted here without the module having to exist.
RICH_CONFIG: Dict[str, Any] = {
    "$schema": "https://example.invalid/boundary.config.schema.json",
    "project": "café",
    "providers": [
        {
            "module": "example_providers.digest",
            "class": "DigestProvider",
            "name": "custom.digest",
        }
    ],
    "defaults": {"compat_mode": "major"},
    "components": {
        "svc": {
            "path": "svc",
            "note": "naïve",
            "ecosystem": "python",
            "vendored_copies": [],
            "boundary": {"provider": "leaf", "paths": []},
        }
    },
    "slices": {
        "all": {"description": "everything", "mode": "exact", "components": ["svc"]}
    },
}

#: Rejections `add` and `remove` make, and the message each prints. Every one
#: of them must leave the file byte-identical.
REFUSED_MUTATIONS = {
    "a name already in the config": (
        ("add", "svc", "extra"),
        "ERROR: Component 'svc' already exists in config.",
    ),
    "a path that climbs out of the repository": (
        ("add", "other", "../outside"),
        "ERROR: Invalid component path: '../outside'",
    ),
    "an empty path": (
        ("add", "other", ""),
        "ERROR: Invalid component path: ''",
    ),
    "a name carrying the filter delimiter": (
        ("add", "a,b", "extra"),
        "ERROR: Component name 'a,b' is not addressable: must not contain ',' "
        "because CLI, GitHub Action, and GitLab component filters are "
        "comma-separated. Rename it so --components and CI filters can select "
        "it unambiguously.",
    ),
    "a name with trailing whitespace": (
        ("add", "trail ", "extra"),
        "ERROR: Component name 'trail ' is not addressable: must not have "
        "leading or trailing whitespace. Rename it so --components and CI "
        "filters can select it unambiguously.",
    ),
    "removal of a component that is not there": (
        ("remove", "ghost"),
        "ERROR: Component 'ghost' not found in config.",
    ),
}

#: The spellings a checkout can legitimately arrive in. None of them is what
#: `dump_config` emits, which is the whole point.
NON_CANONICAL_SPELLINGS = {
    "four-space indent": {"indent": 4, "crlf": False, "ascii_only": False},
    "CRLF line endings": {"indent": 2, "crlf": True, "ascii_only": False},
    "one line, no indent": {"indent": None, "crlf": False, "ascii_only": False},
    "literal non-ASCII at two-space indent": {
        "indent": 2,
        "crlf": False,
        "ascii_only": False,
    },
}


def _canonical_bytes(value: Dict[str, Any]) -> bytes:
    """What `dump_config` must produce, spelled with the standard library.

    `_bounded_json_dumps(value, indent=2)` defaults to `ensure_ascii=True`, and
    `_write_text_atomic` opens with `newline="\\n"`, so this is the oracle:
    stdlib `json.dumps` at the same indent, one trailing newline, UTF-8, LF.
    """
    return (json.dumps(value, indent=2) + "\n").encode("utf-8")


def _write_spelling(
    path: Path, value: Dict[str, Any], *, indent, crlf: bool, ascii_only: bool
) -> bytes:
    text = json.dumps(value, indent=indent, ensure_ascii=ascii_only) + "\n"
    if crlf:
        text = text.replace("\n", "\r\n")
    data = text.encode("utf-8")
    path.write_bytes(data)
    return data


#: Text for the two free-form fields the rewrite must carry through. The
#: alphabet mixes ASCII with codepoints at or above U+007F because those are
#: what `ensure_ascii=True` rewrites. `project` must survive validation, which
#: rejects both an empty value and one with surrounding whitespace.
PROJECT_TEXT = st.text(alphabet="abXY 019éüπ漢", min_size=1, max_size=12).filter(
    lambda value: value == value.strip() != ""
)

NOTE_TEXT = st.text(alphabet="abXY 019éüπ漢", min_size=0, max_size=12)


class ConfigRewriteFidelityTests(unittest.TestCase):
    """OBL-PROVIDERS-062: `add` then `remove` reserializes, it does not patch."""

    @classmethod
    def setUpClass(cls):
        scene = Scenario("p")
        scene.component("svc", path="svc", provider="leaf")
        scene.file("svc/content.txt", "x\n")
        scene.file("extra/content.txt", "y\n")
        scene.commit()
        cls.scene = scene
        cls.config_path = scene.root / "boundary.config.json"

    @classmethod
    def tearDownClass(cls):
        cls._clear_git_config_caches()
        cls.scene.close()

    @staticmethod
    def _clear_git_config_caches() -> None:
        """Both override readers are lru_cached on the resolved root string.

        `run_cli_in_process` runs `core.main()` in this interpreter, so unlike
        the subprocess runner it leaves entries in those caches keyed on a
        temporary directory this class is about to delete. Clearing at both
        ends matches what the rest of the suite does.
        """
        _ambient_worktree_config_overrides.cache_clear()
        _repository_filter_config_overrides.cache_clear()

    def setUp(self):
        self._clear_git_config_caches()

    def tearDown(self):
        self._clear_git_config_caches()

    def _round_trip(self, value: Dict[str, Any], **spelling) -> Tuple[bytes, bytes]:
        before = _write_spelling(self.config_path, value, **spelling)
        added = run_cli_in_process(self.scene.root, "add", "extra", "extra")
        self.assertEqual(added.returncode, EXIT_OK, added.stderr)
        removed = run_cli_in_process(self.scene.root, "remove", "extra")
        self.assertEqual(removed.returncode, EXIT_OK, removed.stderr)
        return before, self.config_path.read_bytes()

    @SLOW_PROFILE
    @given(
        PROJECT_TEXT,
        NOTE_TEXT,
        st.sampled_from(sorted(NON_CANONICAL_SPELLINGS)),
        st.booleans(),
    )
    def test_the_rewrite_is_canonical_json_whatever_the_file_looked_like(
        self, project, note, spelling, reverse_keys
    ):
        value = json.loads(json.dumps(RICH_CONFIG))
        value["project"] = project
        value["components"]["svc"]["note"] = note
        if reverse_keys:
            value = dict(reversed(list(value.items())))
        _before, after = self._round_trip(value, **NON_CANONICAL_SPELLINGS[spelling])
        self.assertEqual(after, _canonical_bytes(value))
        self.assertEqual(json.loads(after.decode("utf-8")), value)
        self.assertEqual(list(json.loads(after.decode("utf-8"))), list(value))
        self.assertNotIn(b"\r", after)

    def test_a_config_already_in_canonical_form_round_trips_byte_identically(self):
        """The premise, and the only case OBL-LOCKFILE-051 describes correctly."""
        before, after = self._round_trip(
            RICH_CONFIG, indent=2, crlf=False, ascii_only=True
        )
        self.assertEqual(before, _canonical_bytes(RICH_CONFIG))
        self.assertEqual(after, before)

    def test_a_config_not_already_canonical_comes_back_reformatted(self):
        """Known-good divergence from OBL-LOCKFILE-051, pinned by example.

        That obligation says `add NAME PATH` followed by `remove NAME` must
        restore the original bytes. It does not, and cannot: the file is
        reserialized through `dump_config`, so four-space indent becomes two,
        CRLF becomes LF, and every codepoint at or above U+007F becomes a
        `\\uXXXX` escape. The value is identical; the bytes are not.
        """
        for label, spelling in NON_CANONICAL_SPELLINGS.items():
            with self.subTest(spelling=label):
                before, after = self._round_trip(RICH_CONFIG, **spelling)
                self.assertNotEqual(after, before)
                self.assertEqual(after, _canonical_bytes(RICH_CONFIG))
                self.assertEqual(json.loads(after.decode("utf-8")), RICH_CONFIG)

    def test_the_rewrite_escapes_non_ascii_and_normalizes_line_endings(self):
        before, after = self._round_trip(
            RICH_CONFIG, indent=2, crlf=True, ascii_only=False
        )
        self.assertIn("café".encode("utf-8"), before)
        self.assertIn(b"\r\n", before)
        self.assertNotIn("café".encode("utf-8"), after)
        self.assertNotIn(b"\r", after)
        self.assertIn(b'"project": "caf\\u00e9"', after)
        self.assertIn(b'"note": "na\\u00efve"', after)

    def test_untouched_fields_and_key_order_survive_the_rewrite(self):
        reversed_value = dict(reversed(list(RICH_CONFIG.items())))
        _before, after = self._round_trip(
            reversed_value, indent=4, crlf=False, ascii_only=False
        )
        restored = json.loads(after.decode("utf-8"))
        self.assertEqual(list(restored), list(reversed_value))
        self.assertEqual(
            list(restored["components"]["svc"]),
            ["path", "note", "ecosystem", "vendored_copies", "boundary"],
        )
        for field in ("$schema", "providers", "defaults", "slices"):
            self.assertEqual(restored[field], RICH_CONFIG[field])
        self.assertEqual(restored["components"]["svc"], RICH_CONFIG["components"]["svc"])

    def test_a_legal_add_really_does_rewrite_the_file(self):
        """The premise for every all-or-nothing assertion below."""
        before = _write_spelling(
            self.config_path, RICH_CONFIG, indent=2, crlf=False, ascii_only=True
        )
        result = run_cli_in_process(self.scene.root, "add", "extra", "extra")
        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        after = self.config_path.read_bytes()
        self.assertNotEqual(after, before)
        self.assertEqual(
            list(json.loads(after.decode("utf-8"))["components"]), ["svc", "extra"]
        )
        self.assertEqual(
            run_cli_in_process(self.scene.root, "remove", "extra").returncode, EXIT_OK
        )

    def test_every_refused_mutation_leaves_the_file_byte_identical(self):
        before = _write_spelling(
            self.config_path, RICH_CONFIG, indent=4, crlf=False, ascii_only=False
        )
        for label, (argv, message) in REFUSED_MUTATIONS.items():
            with self.subTest(refusal=label):
                self.config_path.write_bytes(before)
                result = run_cli_in_process(self.scene.root, *argv)
                self.assertEqual(result.returncode, EXIT_USAGE, result.stderr)
                self.assertEqual(result.stderr.strip(), message)
                self.assertEqual(self.config_path.read_bytes(), before)

    @requires_yaml
    def test_the_json_only_refusal_is_decided_before_the_file_is_parsed(self):
        """A YAML config that cannot even be parsed still gets the JSON message.

        That ordering is the whole guarantee: the command refuses on the
        extension, so it never loads, never validates, and never writes. The
        argument only works if the document really is unparseable, so that is
        asserted first rather than assumed, and the message it would have
        produced is named - so the refusal is not merely the right text, it is
        provably the text of a command that never reached the parser.
        """
        self.config_path.unlink()
        broken = b"project: p\ncomponents: [\n"
        yaml_path = self.scene.root / "boundary.config.yaml"
        yaml_path.write_bytes(broken)
        with self.assertRaises(ConfigError) as caught:
            parse_config_text(broken.decode("utf-8"), yaml_path)
        parse_failure = str(caught.exception)
        self.assertEqual(
            parse_failure,
            f"YAML parse error in {yaml_path}: ParserError at line 3, column 1",
        )
        try:
            for command in ("add", "remove"):
                with self.subTest(command=command):
                    argv = (command, "extra", "extra") if command == "add" else (command, "svc")
                    result = run_cli_in_process(self.scene.root, *argv)
                    self.assertEqual(result.returncode, EXIT_USAGE)
                    self.assertEqual(
                        result.stderr.strip(),
                        f"ERROR: `boundver {command}` only writes JSON configs. "
                        "Use boundary.config.json or edit your YAML/TOML config "
                        "directly.",
                    )
                    self.assertNotIn("ParserError", result.stderr)
                    self.assertEqual(yaml_path.read_bytes(), broken)
        finally:
            yaml_path.unlink()
            _write_spelling(
                self.config_path, RICH_CONFIG, indent=2, crlf=False, ascii_only=True
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
