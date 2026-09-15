"""Six places where boundver's answer has to survive leaving the process.

Five of these obligations are about a boundary crossing. A component name is
repository-controlled text that boundver prints into a CI log; an Action output
is repository-controlled text that boundver writes into a file the runner will
parse; a lockfile is text a reviewer will diff; a config is a document two
different validation engines have to agree about. In every case the thing that
matters is not what boundver computed but what the reader on the other side
does with the bytes, and the recurring way these tests go wrong is to model the
reader the way the code models it. So each check here carries its own model of
the far side, written from that reader's documented rules rather than from
boundver's source: the Actions runner strips leading whitespace before it looks
for ``::``, and it breaks ``$GITHUB_OUTPUT`` on CR, LF and CRLF and nothing
else. Both models disagree with the code in at least one place, which is the
whole point of writing them separately.

Two of those disagreements turned out to be real. Every existing assertion in
the suite tests ``line.startswith("::")`` on the raw line, and every emitter
that prefixes boundver's own indentation slips underneath it: a component named
``::error::pwn`` produces ``    ::error::pwn -> components: downstream``, which
the runner trims and executes. Six of the sixteen subcommands do it, so that
one is pinned as a divergence with the current behaviour recorded beside it.
The second is smaller: ``_schema_engine_errors`` adds its lines to the
hand-rolled ones rather than replacing them, so the *set* of reported errors is
not identical with and without the ``schema`` extra even though the accept
or reject decision always is. The transport half went the other way. The
register suspected that ``value.splitlines()`` disagreeing with the runner's
line splitting was an injection route; it is a real disagreement, but it runs
in the safe direction, because ``splitlines`` refines the runner's partition
and so can only exclude more delimiter candidates than it needs to. Saying that
with a fuzz over the separator alphabet is worth more than leaving it suspected.

The awkward part of the fixtures was cost. ``validate_config`` under its
default ``working-tree`` source captures a Git snapshot on every call, which
makes a 70-row grammar table a 40-second test; under ``index`` the same call is
seven times cheaper and validates the same document, so the tables run there.
The CLI sweep needs a repository where a reconciled lock exists at two
consecutive commits (or ``review`` refuses both endpoints) and an uncommitted
edit on top (or ``verify`` reports no drift and prints no consumer impact), and
it needs sixteen commands not to tread on each other's config file, so each one
runs against a fresh copytree of one repository built once for the class.

Covers OBL-GIT-SOURCE-057, OBL-GIT-SOURCE-058, OBL-GIT-SOURCE-065,
OBL-GIT-SOURCE-066, OBL-GIT-SOURCE-069 and OBL-GIT-SOURCE-070.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path, PurePosixPath
from typing import Dict, List, Tuple
from unittest import mock

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

import boundver._config as config_module
import boundver._output as output_module
from boundver._cli_parser import build_parser
from boundver._config import _load_config_schema, validate_config
from boundver._config_contract import (
    COMPONENT_IDENTIFIER_PATTERN,
    component_identifier_problem,
)
from boundver._git import changed_components_since_ref, changed_paths_since_ref
from boundver._hashing import source_tree_digest
from boundver._lockfile import LOCKFILE_SCHEMA_URL, dump_lockfile

from tests._parity import run_cli
from tests._scenarios import SOURCE_MODES, Scenario, requires_symlinks

REPO_ROOT = Path(__file__).resolve().parents[1]

#: A component name the config schema accepts (`componentIdentifier` forbids
#: only a comma and surrounding whitespace) that reads as a GitHub Actions
#: workflow command once a log line is trimmed.
HOSTILE_COMPONENT = "::error::pwn"

#: The same trick applied to an external consumer label, which travels through
#: a different emitter than the component name does.
HOSTILE_EXTERNAL = "::warning::ext"

#: How the GitHub Actions runner ends a line: CR, LF or CRLF, and nothing else.
#: Python's ``str.splitlines`` also breaks on VT, FF, FS, GS, RS, NEL, U+2028
#: and U+2029, which is exactly the disagreement OBL-GIT-SOURCE-058 is about.
_RUNNER_LINE_BREAK = re.compile("\r\n|\n|\r")

#: Characters that ``str.splitlines`` treats as line breaks but the runner does
#: not, plus the two the runner does honour, plus text that could be mistaken
#: for the transport's own syntax.
TRANSPORT_ALPHABET = (
    "a",
    "b",
    "=",
    "<<",
    "\n",
    "\r",
    "\x0b",
    "\x0c",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    "\u2028",
    "\u2029",
    "BOUNDVER_OUTPUT_",
    "issues",
    "\U0001f600",
)


def _load_exporter():
    """Import ``scripts/export_action_outputs.py``, which is not a package."""
    path = REPO_ROOT / "scripts" / "export_action_outputs.py"
    spec = importlib.util.spec_from_file_location("chunk22_exporter", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runner_lines(text: str) -> List[str]:
    """Split *text* the way the Actions runner reads ``$GITHUB_OUTPUT``."""
    return _RUNNER_LINE_BREAK.split(text)


def runner_parse(text: str) -> Dict[str, str]:
    """Model the runner's file-command parser.

    Whichever of ``=`` and ``<<`` appears first on a line decides the form; a
    heredoc collects following lines until one equals the delimiter exactly,
    and an unterminated heredoc is a hard error rather than a partial value.
    """
    lines = runner_lines(text)
    if lines and lines[-1] == "":
        lines = lines[:-1]
    parsed: Dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if line == "":
            continue
        equals = line.find("=")
        heredoc = line.find("<<")
        if equals >= 0 and (heredoc < 0 or equals < heredoc):
            name, value = line.split("=", 1)
            parsed[name] = value
            continue
        if heredoc >= 0:
            name, delimiter = line.split("<<", 1)
            collected: List[str] = []
            while index < len(lines) and lines[index] != delimiter:
                collected.append(lines[index])
                index += 1
            if index >= len(lines):
                raise AssertionError(f"unterminated heredoc for {name!r}")
            index += 1
            parsed[name] = "\n".join(collected)
            continue
        raise AssertionError(f"unparseable output line {line!r}")
    return parsed


def runner_value_of(value: str) -> str:
    """What the runner reads back for *value*, by its own line rules.

    The writer emits the value followed by a newline, so the runner sees the
    value's own lines and one trailing empty one. Folding CR and CRLF to LF is
    the runner's behaviour, not boundver's, which is why the model states it
    rather than importing it.
    """
    return "\n".join(runner_lines(value + "\n")[:-1])


def _subcommand_names() -> List[str]:
    """Every subcommand the parser offers, read from the parser itself."""
    parser = build_parser(version="0.0.0", epilog="")
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices)
    raise AssertionError("the CLI parser exposes no subparsers")


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-057 - no emitted line may read as a workflow command
# ---------------------------------------------------------------------------

#: One runnable invocation per subcommand the parser offers. The keys are
#: checked against the parser at runtime, so a new subcommand fails the suite
#: until someone decides how to exercise it rather than being skipped.
CLI_INVOCATIONS: Dict[str, Tuple[str, ...]] = {
    "generate": ("generate",),
    "verify": ("verify", "--source", "working-tree"),
    "review": ("review", "HEAD~1..HEAD"),
    "status": ("status", "--source", "working-tree"),
    "validate-config": ("validate-config",),
    "check-config": ("check-config",),
    "explain": ("explain", HOSTILE_COMPONENT, "--source", "working-tree"),
    "why": ("why", HOSTILE_COMPONENT, "--source", "working-tree"),
    "discover": ("discover",),
    "slice": ("slice", "everything"),
    "diff": ("diff", "boundary.lock.json", "boundary.lock.json"),
    "migrate-lock": ("migrate-lock", "--dry-run"),
    "completions": ("completions", "--shell", "bash"),
    "coverage": ("coverage", "--source", "working-tree"),
    "init": ("init", "--force"),
    "add": ("add", "added-component", "svc2"),
    "remove": ("remove", HOSTILE_COMPONENT),
    "record-derivation": ("record-derivation", "missing"),
}

#: No supported command may emit a GitHub workflow-command line.
FORGED_LINES_TODAY: Dict[str, Tuple[str, ...]] = {}

_DIGEST_PREVIEW = re.compile(r"\b[0-9a-f]{12}\b")


def _forged(rendered: str) -> List[str]:
    """Lines the Actions runner would execute, digests normalised."""
    return [
        _DIGEST_PREVIEW.sub("<digest>", line)
        for line in rendered.splitlines()
        if line.lstrip().startswith("::")
    ]


class WorkflowCommandForgeryTests(unittest.TestCase):
    """OBL-GIT-SOURCE-057: nothing boundver prints may become a command."""

    scene: Scenario
    rendered: Dict[str, str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = Scenario()
        scene = cls.scene
        scene.component(
            HOSTILE_COMPONENT,
            path="svc",
            provider="path-hash",
            boundary=["*.txt"],
            consumers=["downstream"],
            external_consumers=[HOSTILE_EXTERNAL],
        )
        scene.component("downstream", path="downstream", provider="leaf")
        scene.slice("everything", components=[HOSTILE_COMPONENT, "downstream"])
        scene.file("svc/contract.txt", "first revision\n")
        scene.file("downstream/main.py", "x\n")
        scene.file("svc2/other.txt", "y\n")
        scene.commit("base")
        # A reconciled lock at two consecutive commits, because `review`
        # refuses an endpoint whose lock does not match its own tree.
        run_cli(scene.root, "generate")
        scene.git("add", "--all")
        scene.git("commit", "-m", "lock")
        scene.file("svc/contract.txt", "second revision\n")
        scene.git("add", "--all")
        run_cli(scene.root, "generate", "--source", "index")
        scene.git("add", "--all")
        scene.git("commit", "-m", "revision")
        # Uncommitted drift, so `verify` and `why` reach consumer impact.
        scene.file("svc/contract.txt", "uncommitted drift\n")
        cls.rendered = {}
        cls.exit_codes: Dict[str, int] = {}
        for label, argv in CLI_INVOCATIONS.items():
            with tempfile.TemporaryDirectory() as copy:
                target = Path(copy) / "repo"
                shutil.copytree(scene.root, target)
                outcome = run_cli(target, *argv)
            cls.rendered[label] = outcome.stdout + outcome.stderr
            cls.exit_codes[label] = outcome.returncode

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    def test_every_subcommand_the_parser_offers_has_an_invocation(self):
        """The checker's enumeration: a new command cannot slip past unrun."""
        self.assertEqual(sorted(CLI_INVOCATIONS), _subcommand_names())

    def test_the_hostile_name_is_a_name_the_config_schema_accepts(self):
        """Premise: the attack needs a legal component name, not a rejected one."""
        self.assertIsNone(component_identifier_problem(HOSTILE_COMPONENT))
        self.assertIsNone(component_identifier_problem(HOSTILE_EXTERNAL))
        self.assertEqual(self.exit_codes["validate-config"], 0)

    def test_the_detector_recognises_a_line_the_runner_would_execute(self):
        """Premise: the lstrip test finds what a raw startswith test misses."""
        indented = "    ::error::pwn -> components: downstream"
        self.assertFalse(indented.startswith("::"))
        self.assertEqual(_forged(indented), [indented])
        self.assertEqual(_forged("Components: ::error::pwn, other"), [])

    def test_the_hostile_name_reaches_the_output_of_several_commands(self):
        """Premise: an absence proved over output that never held the name
        would prove nothing at all."""
        escaped_name = "\\x3a:error::pwn"
        carrying = sorted(
            label
            for label, text in self.rendered.items()
            if HOSTILE_COMPONENT in text or escaped_name in text
        )
        self.assertEqual(
            carrying,
            ["explain", "generate", "remove", "review", "slice", "status",
             "verify", "why"],
        )

    def test_a_value_printed_as_a_whole_line_is_still_escaped(self):
        """Premise: the guard in `_display_text` exists and does fire; what
        fails is only its placement relative to boundver's own indent."""
        buffer = io.StringIO()
        output_module.safe_print(HOSTILE_COMPONENT, file=buffer)
        self.assertEqual(buffer.getvalue(), "\\x3a:error::pwn\n")
        self.assertEqual(_forged(buffer.getvalue()), [])

    def test_no_command_emits_a_line_the_actions_runner_would_execute(self):
        """Indentation cannot hide a forged workflow-command prefix."""
        offenders = {
            label: _forged(text)
            for label, text in self.rendered.items()
            if _forged(text)
        }
        self.assertEqual(offenders, {})

    def test_the_expected_forgery_inventory_is_empty(self):
        observed = {
            label: _forged(text)
            for label, text in self.rendered.items()
            if _forged(text)
        }
        self.assertEqual(
            sorted(observed), sorted(FORGED_LINES_TODAY), self.rendered.keys()
        )
        for label, lines in FORGED_LINES_TODAY.items():
            with self.subTest(command=label):
                self.assertEqual(observed[label], list(lines))

    def test_no_command_emits_a_forged_command_at_column_zero(self):
        """The weaker invariant the suite already had still holds everywhere,
        which is why the stronger one went unnoticed."""
        for label, text in self.rendered.items():
            with self.subTest(command=label):
                self.assertEqual(
                    [line for line in text.splitlines() if line.startswith("::")],
                    [],
                )

    def test_a_joined_print_escapes_a_hostile_value_in_every_position(self):
        """`sep=""` is trusted layout, so the guard must survive the join."""
        buffer = io.StringIO()
        output_module.safe_print(
            HOSTILE_COMPONENT, HOSTILE_EXTERNAL, sep="", file=buffer
        )
        self.assertEqual(
            buffer.getvalue(), "\\x3a:error::pwn\\x3a:warning::ext\n"
        )
        self.assertEqual(_forged(buffer.getvalue()), [])


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-058 - Action outputs resist injection and stay in budget
# ---------------------------------------------------------------------------


class _FixedDigest:
    """A SHA-256 stand-in, so a delimiter collision can be constructed."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def hexdigest(self) -> str:
        return "f" * 64


class _FixedHashlib:
    @staticmethod
    def sha256(*args: object, **kwargs: object) -> _FixedDigest:
        return _FixedDigest()


class ActionOutputTransportTests(unittest.TestCase):
    """OBL-GIT-SOURCE-058: one name in, one name out, inside the budget."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.exporter = _load_exporter()
        cls.declared_outputs = cls._action_yml_outputs()

    @staticmethod
    def _action_yml_outputs() -> List[str]:
        """The output names action.yml publishes, read at runtime."""
        text = (REPO_ROOT / "action.yml").read_text(encoding="utf-8")
        body = text.split("\noutputs:\n", 1)[1].split("\nruns:", 1)[0]
        return sorted(
            match.group(1)
            for match in re.finditer(r"^  ([a-z0-9-]+):$", body, re.MULTILINE)
        )

    def _export(self, payload_text: str, *, limit: int, operation: str = "verify"):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            result = base / "result.json"
            result.write_text(payload_text, encoding="utf-8")
            github_output = base / "github_output"
            github_output.write_text("", encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                truncated = self.exporter.export_outputs(
                    result,
                    github_output,
                    operation=operation,
                    max_value_utf16_bytes=limit,
                )
            raw = github_output.read_text(encoding="utf-8")
        return truncated, runner_parse(raw), raw

    # ---- premises ---------------------------------------------------------

    def test_the_runner_model_reads_a_hand_forged_second_output(self):
        """Premise: the parser this file trusts really does see an injected
        name, so asserting one name is not asserting a blind spot."""
        forged = (
            "issues<<D\n"
            "harmless\n"
            "D\n"
            "deploy-everything=true\n"
        )
        self.assertEqual(
            runner_parse(forged),
            {"issues": "harmless", "deploy-everything": "true"},
        )

    def test_the_runner_model_and_splitlines_disagree_where_expected(self):
        """Premise for the safe-direction argument: the two really do differ.
        `_delimiter` builds its occupied set with `splitlines`, which breaks on
        five separators the runner ignores."""
        value = "a\x0bb\u2028c"
        self.assertEqual(runner_lines(value), [value])
        self.assertEqual(value.splitlines(), ["a", "b", "c"])
        self.assertNotEqual(runner_lines(value), value.splitlines())

    @settings(
        max_examples=250,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(pieces=st.lists(st.sampled_from(TRANSPORT_ALPHABET), max_size=14))
    def test_splitlines_refines_the_runner_partition_rather_than_coarsening_it(
        self, pieces
    ):
        """Why the disagreement is safe rather than an injection route. A
        delimiter contains no separator of either kind, so if the runner would
        see one as a whole line then `splitlines` produces it too, and the
        candidate was excluded. The dangerous direction would be a runner line
        that `splitlines` never yields."""
        value = "".join(pieces)
        python_lines = set(value.splitlines())
        for line in runner_lines(value):
            if line and line == "".join(line.splitlines()):
                self.assertIn(line, python_lines, value)

    def test_a_delimiter_collision_extends_the_delimiter(self):
        """The `while candidate in occupied` loop cannot be reached with a real
        SHA-256, because the digest covers the value that would have to contain
        it. Pinning the digest is the only way to enter the branch."""
        colliding = "BOUNDVER_OUTPUT_" + "f" * 64
        with mock.patch.object(self.exporter, "hashlib", _FixedHashlib):
            self.assertEqual(
                self.exporter._delimiter("issues", "harmless"), colliding
            )
            self.assertEqual(
                self.exporter._delimiter("issues", f"a\n{colliding}\nb"),
                colliding + "X",
            )
            self.assertEqual(
                self.exporter._delimiter(
                    "issues", f"{colliding}\n{colliding}X"
                ),
                colliding + "XX",
            )
            handle = io.StringIO()
            # Without the loop this value terminates its own heredoc and the
            # runner then reads the next line as a second output.
            value = f"a\n{colliding}\nforged-output=1"
            self.exporter._append_output(handle, "issues", value)
            parsed = runner_parse(handle.getvalue())
        self.assertEqual(sorted(parsed), ["issues"])
        self.assertEqual(parsed["issues"], value)

    # ---- the transport property ------------------------------------------

    @settings(
        max_examples=250,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        pieces=st.lists(st.sampled_from(TRANSPORT_ALPHABET), max_size=14),
        name=st.sampled_from(("issues", "observations", "result-file")),
    )
    def test_no_value_can_forge_a_second_output_name(self, pieces, name):
        value = "".join(pieces)
        handle = io.StringIO()
        self.exporter._append_output(handle, name, value)
        written = handle.getvalue()
        parsed = runner_parse(written)
        self.assertEqual(sorted(parsed), [name], written)
        self.assertEqual(parsed[name], runner_value_of(value), written)
        first = runner_lines(written)[0]
        if "<<" in first:
            delimiter = first.split("<<", 1)[1]
            self.assertNotIn(delimiter, runner_lines(value))

    def test_a_carriage_return_does_not_survive_the_transport(self):
        """Pinned, because the obligation asks only that the delimiter hold:
        the runner folds CR and CRLF to LF and drops a trailing CR, so a value
        boundver writes is not always the value a caller reads back."""
        cases = {
            "bare cr": ("a\rb", "a\nb"),
            "crlf": ("a\r\nb", "a\nb"),
            "trailing cr": ("a\r", "a"),
            "lone cr": ("\r", ""),
            "lf survives": ("a\nb", "a\nb"),
            "vertical tab survives": ("a\x0bb", "a\x0bb"),
            "line separator survives": ("a\u2028b", "a\u2028b"),
        }
        for label, (value, expected) in cases.items():
            with self.subTest(case=label):
                handle = io.StringIO()
                self.exporter._append_output(handle, "issues", value)
                self.assertEqual(
                    runner_parse(handle.getvalue())["issues"], expected
                )

    # ---- the size half ----------------------------------------------------

    def test_every_written_output_is_one_the_action_declares(self):
        """The checker: the names are enumerated from the file that was
        written and from action.yml, not from a list kept here."""
        payload = {
            "issues": ["::error::forged", "MISMATCH svc.exact"],
            "observations": ["obs"],
            "consumer_impact": [{"component": "svc"}],
        }
        _, parsed, _ = self._export(
            json.dumps(payload), limit=self.exporter.MAX_VALUE_UTF16_BYTES
        )
        self.assertTrue(parsed)
        for name in sorted(parsed):
            with self.subTest(output=name):
                self.assertIn(name, self.declared_outputs)

    def test_every_sized_output_stays_within_the_utf16_budget(self):
        """The bounded surface is read from the exporter's own constants, so a
        newly bounded output is checked without editing this test."""
        bounded = tuple(self.exporter.SIZED_OUTPUTS) + tuple(
            self.exporter.PLAN_ARRAY_OUTPUTS
        )
        self.assertGreaterEqual(len(bounded), 10)
        limit = 400
        payload = {
            "schema": self.exporter.PLAN_SCHEMA,
            "complete": True,
            "selection": {
                key.replace("-", "_"): [f"component-{index:04d}" for index in range(80)]
                for key in self.exporter.PLAN_ARRAY_OUTPUTS
            },
            "issues": ["MISMATCH " + "x" * 500],
            "observations": ["\U0001f600" * 300],
            "consumer_impact": [{"component": "c" * 500}],
        }
        truncated, parsed, _ = self._export(
            json.dumps(payload), limit=limit, operation="review"
        )
        for name in bounded:
            with self.subTest(output=name):
                self.assertIn(name, parsed)
                self.assertLessEqual(
                    self.exporter._utf16_size(parsed[name]), limit
                )
        self.assertEqual(sorted(truncated), sorted(bounded))

    def test_an_astral_value_is_measured_in_utf16_units(self):
        """A code-point count would have let four times the budget through."""
        payload = {
            "issues": ["\U0001f600" * 100],
            "observations": [],
            "consumer_impact": [],
        }
        text = json.dumps(payload)
        _, parsed, _ = self._export(
            text, limit=self.exporter.MAX_VALUE_UTF16_BYTES
        )
        complete = parsed["issues"]
        self.assertEqual(len(complete), 100)
        self.assertEqual(self.exporter._utf16_size(complete), 400)
        truncated, parsed, _ = self._export(text, limit=399)
        self.assertEqual(truncated, ("issues",))
        self.assertEqual(parsed["issues"], self.exporter.TRUNCATION_MARKER)

    def test_a_value_exactly_at_the_budget_is_kept_and_one_over_is_not(self):
        payload = {
            "issues": [
                "::error::forged annotation attempt",
                "MISMATCH svc.exact: astral " + "\U0001f600" * 20,
                "line\nwith\nnewline and \r carriage",
            ],
            "observations": [],
            "consumer_impact": [],
        }
        text = json.dumps(payload)
        _, parsed, _ = self._export(
            text, limit=self.exporter.MAX_VALUE_UTF16_BYTES
        )
        complete = parsed["issues"]
        exact = self.exporter._utf16_size(complete)
        self.assertEqual(exact, 282)
        self.assertGreater(exact, self.exporter._utf16_size(
            self.exporter.TRUNCATION_MARKER
        ))
        at_limit_truncated, at_limit, _ = self._export(text, limit=exact)
        self.assertEqual(at_limit_truncated, ())
        self.assertEqual(at_limit["issues"], complete)
        over_truncated, over, _ = self._export(text, limit=exact - 1)
        self.assertEqual(over_truncated, ("issues",))
        self.assertLessEqual(
            self.exporter._utf16_size(over["issues"]), exact - 1
        )

    def test_a_lone_surrogate_is_escaped_rather_than_written(self):
        """Premise first: an unescaped lone surrogate cannot reach the file at
        all, so escaping is the only way the value survives the transport."""
        with self.assertRaises(UnicodeEncodeError):
            "lone \ud800 surrogate".encode("utf-8")
        self.assertEqual(
            self.exporter._utf16_size("\ud800"), 2, "one UTF-16 code unit"
        )
        payload = (
            '{"issues": ["lone \\ud800 surrogate"], '
            '"observations": [], "consumer_impact": []}'
        )
        _, parsed, raw = self._export(
            payload, limit=self.exporter.MAX_VALUE_UTF16_BYTES
        )
        self.assertEqual(parsed["issues"], "lone \\ud800 surrogate")
        self.assertNotIn("\ud800", raw)


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-065 - --changed-from is reporting, never a shortcut
# ---------------------------------------------------------------------------

#: Component roots the generated configs draw from. `alpha` and `alphabet`
#: share a string prefix but not a path boundary; `gamma` matches nothing.
CANDIDATE_ROOTS = (
    "alpha",
    "alphabet",
    "beta/nested",
    "beta",
    "zzz-outside",
    "gamma",
)

#: The files each fixture repository modifies against its base commit.
CHANGED_FILES = (
    "alpha/a.txt",
    "alphabet/b.txt",
    "beta/nested/c.txt",
    "zzz-outside/d.txt",
)

#: Names boundver treats as the configuration document itself.
CONFIG_FILE_NAMES = frozenset(
    {
        "boundary.config.json",
        "boundary.config.yaml",
        "boundary.config.yml",
        "boundary.config.toml",
    }
)


def expected_selection(components: dict, changed_files: List[str]) -> List[str]:
    """The four documented selection rules, written from the docs.

    `docs/ci-cookbook.md`: a config-file change selects every component.
    `docs/reference.md`: a changed path that maps to no component root is
    treated conservatively as every component, and a `git_tag_prefix`
    component is always in the reporting set because its version can move
    without any path under its root changing.
    """
    if any(PurePosixPath(name).name in CONFIG_FILE_NAMES for name in changed_files):
        return sorted(components)
    tag_versioned = {
        name
        for name, entry in components.items()
        if "git_tag_prefix" in (entry.get("version_source") or {})
    }
    selected = set()
    matched = set()
    for name, entry in components.items():
        root = entry["path"]
        hits = {
            path
            for path in changed_files
            if path == root or path.startswith(root + "/")
        }
        if hits:
            selected.add(name)
            matched |= hits
    if changed_files and matched != set(changed_files):
        return sorted(components)
    return sorted(selected | tag_versioned)


def _selection_repository(*, touch_config: bool) -> Scenario:
    scene = Scenario()
    scene.component("seed", path="alpha", provider="leaf")
    for path in CHANGED_FILES:
        scene.file(path, "original\n")
    scene.file("gamma/untouched.txt", "original\n")
    scene.commit("base")
    for path in CHANGED_FILES:
        scene.file(path, "modified\n")
    if touch_config:
        document = json.loads(
            (scene.root / "boundary.config.json").read_text(encoding="utf-8")
        )
        document["project"] = "renamed"
        (scene.root / "boundary.config.json").write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )
    scene.git("add", "--all")
    return scene


class ChangedFromSelectionTests(unittest.TestCase):
    """OBL-GIT-SOURCE-065: the selection rules, and their irrelevance to the
    verification result."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.states = {
            "config unchanged": _selection_repository(touch_config=False),
            "config changed": _selection_repository(touch_config=True),
        }
        # The base is the commit before the staged edits, which is HEAD here
        # because nothing was committed after them.
        cls.bases = {
            label: scene.head() for label, scene in cls.states.items()
        }
        cls.changed_paths = {
            label: changed_paths_since_ref(scene.root, cls.bases[label], "index")
            for label, scene in cls.states.items()
        }

    @classmethod
    def tearDownClass(cls) -> None:
        for scene in cls.states.values():
            scene.close()

    def test_the_fixture_changed_the_paths_the_oracle_will_be_given(self):
        """Premise: an oracle fed an empty change set would agree trivially."""
        self.assertEqual(
            self.changed_paths["config unchanged"], sorted(CHANGED_FILES)
        )
        self.assertEqual(
            self.changed_paths["config changed"],
            sorted((*CHANGED_FILES, "boundary.config.json")),
        )

    @settings(
        max_examples=40,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        roots=st.lists(
            st.sampled_from(CANDIDATE_ROOTS), unique=True, max_size=5
        ),
        tagged=st.lists(st.booleans(), min_size=6, max_size=6),
        state=st.sampled_from(("config unchanged", "config changed")),
    )
    # Every changed path is covered, so the conservative fall-back stays out of
    # the way and `gamma` can only be selected by the always-select tag rule.
    @example(
        roots=["alpha", "alphabet", "beta", "zzz-outside", "gamma"],
        tagged=[False, False, False, False, True, False],
        state="config unchanged",
    )
    # One changed path maps to no root, so the fall-back must select `gamma`
    # even though nothing under it moved.
    @example(
        roots=["alpha", "gamma"],
        tagged=[False] * 6,
        state="config unchanged",
    )
    # The config-file rule, which short-circuits before either of those.
    @example(
        roots=["alpha", "gamma"],
        tagged=[False] * 6,
        state="config changed",
    )
    @example(roots=[], tagged=[False] * 6, state="config unchanged")
    def test_selection_follows_the_documented_rules(self, roots, tagged, state):
        components = {}
        for index, root in enumerate(roots):
            entry = {
                "path": root,
                "boundary": {"provider": "leaf", "paths": []},
            }
            if tagged[index]:
                entry["version_source"] = {"git_tag_prefix": f"c{index}-v"}
            components[f"c{index}"] = entry
        config = {"project": "p", "components": components}
        scene = self.states[state]
        observed = changed_components_since_ref(
            config, scene.root, self.bases[state], "index"
        )
        self.assertEqual(
            observed,
            expected_selection(components, self.changed_paths[state]),
            (state, roots, tagged),
        )

    def test_a_path_inside_one_root_selects_only_that_component(self):
        """Premise: selection is not simply everything, always."""
        with Scenario() as scene:
            scene.component("alpha", path="alpha", provider="leaf")
            scene.component("beta", path="beta", provider="leaf")
            scene.file("alpha/a.txt", "x\n")
            scene.file("beta/b.txt", "x\n")
            scene.commit("base")
            base = scene.head()
            scene.file("alpha/a.txt", "y\n")
            scene.commit("touch alpha")
            self.assertEqual(
                changed_components_since_ref(
                    scene.config, scene.root, base, "head"
                ),
                ["alpha"],
            )

    def test_a_path_outside_every_root_selects_every_component(self):
        with Scenario() as scene:
            scene.component("alpha", path="alpha", provider="leaf")
            scene.component("beta", path="beta", provider="leaf")
            scene.file("alpha/a.txt", "x\n")
            scene.file("beta/b.txt", "x\n")
            scene.file("orphan/note.md", "x\n")
            scene.commit("base")
            base = scene.head()
            scene.file("orphan/note.md", "y\n")
            scene.commit("touch orphan")
            self.assertEqual(
                changed_components_since_ref(
                    scene.config, scene.root, base, "head"
                ),
                ["alpha", "beta"],
            )

    def test_a_changed_config_file_selects_every_component(self):
        with Scenario() as scene:
            scene.component("alpha", path="alpha", provider="leaf")
            scene.file("alpha/a.txt", "x\n")
            scene.commit("base")
            base = scene.head()
            scene.component("beta", path="beta", provider="leaf")
            scene.file("beta/b.txt", "x\n")
            scene.commit("add beta")
            self.assertEqual(
                changed_components_since_ref(
                    scene.config, scene.root, base, "head"
                ),
                ["alpha", "beta"],
            )

    def test_a_tag_versioned_component_is_selected_even_with_no_change(self):
        with Scenario() as scene:
            scene.component("alpha", path="alpha", provider="leaf")
            scene.component(
                "tagged",
                path="tagged",
                provider="leaf",
                version_source={"git_tag_prefix": "tagged-v"},
            )
            scene.file("alpha/a.txt", "x\n")
            scene.file("tagged/t.txt", "x\n")
            scene.commit("base")
            base = scene.head()
            scene.commit("empty")
            self.assertEqual(
                changed_components_since_ref(
                    scene.config, scene.root, base, "head"
                ),
                ["tagged"],
            )

    def test_a_root_component_absorbs_every_changed_path(self):
        """A component at `.` matches everything, so the conservative
        fall-back never fires and the selection stays precise."""
        with Scenario() as scene:
            scene.component("whole", path=".", provider="leaf")
            scene.component("alpha", path="alpha", provider="leaf")
            scene.file("alpha/a.txt", "x\n")
            scene.file("orphan/note.md", "x\n")
            scene.commit("base")
            base = scene.head()
            scene.file("orphan/note.md", "y\n")
            scene.commit("touch orphan")
            self.assertEqual(
                changed_components_since_ref(
                    scene.config, scene.root, base, "head"
                ),
                ["whole"],
            )

    def test_changed_from_changes_neither_the_issues_nor_the_exit_code(self):
        with Scenario() as scene:
            scene.component(
                "alpha", path="alpha", provider="path-hash", boundary=["*.txt"]
            )
            scene.component("beta", path="beta", provider="leaf")
            scene.file("alpha/a.txt", "x\n")
            scene.file("beta/b.py", "x\n")
            scene.commit("base")
            base = scene.head()
            run_cli(scene.root, "generate")
            scene.git("add", "--all")
            scene.git("commit", "-m", "lock")
            scene.file("alpha/a.txt", "drifted\n")
            scene.commit("drift")
            plain = run_cli(scene.root, "verify", "--format", "json")
            scoped = run_cli(
                scene.root, "verify", "--changed-from", base, "--format", "json"
            )
            self.assertEqual(plain.returncode, 4)
            self.assertEqual(plain.returncode, scoped.returncode)
            plain_doc = json.loads(plain.stdout)
            scoped_doc = json.loads(scoped.stdout)
            differing = sorted(
                key
                for key in set(plain_doc) | set(scoped_doc)
                if plain_doc.get(key) != scoped_doc.get(key)
            )
            self.assertEqual(differing, ["changed_components"])
            self.assertTrue(plain_doc["issues"])

    def test_a_zero_match_result_still_prints_an_explicit_zero_line(self):
        with Scenario() as scene:
            scene.component("alpha", path="alpha", provider="leaf")
            scene.file("alpha/a.txt", "x\n")
            scene.commit("base")
            run_cli(scene.root, "generate")
            scene.git("add", "--all")
            scene.git("commit", "-m", "lock")
            human = run_cli(scene.root, "verify", "--changed-from", "HEAD")
            self.assertEqual(human.returncode, 0)
            self.assertIn(
                "Changed component paths (0): none; "
                "validating full lock integrity.",
                human.stdout,
            )


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-066 - a regenerated lock must not churn
# ---------------------------------------------------------------------------

#: The immutable publication a persisted lock points at, independent of the
#: release doing the writing.
CANONICAL_SCHEMA_URL = (
    "https://raw.githubusercontent.com/yzm1/boundver/v0.16.0/"
    "spec/boundary.lock.schema.json"
)


class LockStabilityTests(unittest.TestCase):
    """OBL-GIT-SOURCE-066: identical content, identical bytes."""

    def _repository(self) -> Scenario:
        scene = Scenario()
        scene.component(
            "svc", path="svc", provider="path-hash", boundary=["**/*.txt"]
        )
        scene.component("leafy", path="leafy", provider="leaf")
        scene.slice("all", components=["svc", "leafy"])
        scene.file("svc/api/v1.txt", "openapi\n")
        scene.file("svc/api/v2.txt", "openapi\r\n", crlf=True)
        scene.file("leafy/main.py", "x\n")
        scene.commit("base")
        return scene

    def test_the_schema_url_is_pinned_to_the_canonical_publication(self):
        self.assertEqual(LOCKFILE_SCHEMA_URL, CANONICAL_SCHEMA_URL)
        self.assertIn("/v0.16.0/", LOCKFILE_SCHEMA_URL)
        with self._repository() as scene:
            self.assertEqual(scene.generate()["$schema"], CANONICAL_SCHEMA_URL)

    def test_a_generated_lock_records_nothing_about_the_running_release(self):
        """A version, a timestamp or a commit id in the document would dirty
        every lock on upgrade, which is the churn the obligation forbids.

        The tag-pinned ``$schema`` URL identifies the lock format publication;
        it is not generator metadata and is intentionally allowed to contain
        the release that introduced the format.
        """
        import boundver

        with self._repository() as scene:
            before = dump_lockfile(scene.generate())
            self.assertNotIn(scene.head(), before)
            saved = boundver.__version__
            try:
                boundver.__version__ = "99.99.99"
                after = dump_lockfile(scene.generate())
            finally:
                boundver.__version__ = saved
            self.assertEqual(before, after)
            self.assertEqual(
                sorted(json.loads(before)),
                [
                    "$schema",
                    "components",
                    "config_contract",
                    "config_digest",
                    "project",
                    "schema",
                    "slices",
                ],
            )

    def test_every_source_mode_produces_byte_identical_lock_text(self):
        """The existing source loop verifies each lock against itself; this
        compares them to each other, which is what a source-mode leak would
        break."""
        with self._repository() as scene:
            self.assertEqual(scene.git("status", "--porcelain"), "")
            rendered = {
                mode: dump_lockfile(scene.generate(source=mode)).encode("utf-8")
                for mode in SOURCE_MODES
            }
            self.assertEqual(len(set(rendered.values())), 1, sorted(rendered))

    def test_the_byte_comparison_notices_a_single_character(self):
        """Premise: the comparison above is a real comparison."""
        with self._repository() as scene:
            original = dump_lockfile(scene.generate())
            tampered = dump_lockfile(
                {**scene.generate(), "project": "scenario "}
            )
            self.assertNotEqual(original.encode("utf-8"), tampered.encode("utf-8"))
            self.assertEqual(len(tampered), len(original) + 1)

    def test_the_serialised_shape_is_two_space_indent_and_a_final_newline(self):
        """The golden test compares parsed dicts, so the rendering itself has
        no pin anywhere else."""
        with self._repository() as scene:
            lock = scene.generate()
            rendered = dump_lockfile(lock)
            self.assertEqual(rendered, json.dumps(lock, indent=2) + "\n")
            self.assertTrue(rendered.endswith("}\n"))
            self.assertFalse(rendered.endswith("\n\n"))
            self.assertTrue(
                rendered.startswith('{\n  "$schema": "'), rendered[:40]
            )


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-069 - the same verdict with and without the extras
# ---------------------------------------------------------------------------

#: Every code point Python's ``str.strip`` removes, which is the set
#: COMPONENT_IDENTIFIER_PATTERN spells out and the set the schema copy has to
#: match. The exotic members are the ones no existing test exercises.
STRIP_CODEPOINTS = tuple(
    chr(code)
    for code in (
        0x09, 0x0A, 0x0B, 0x0C, 0x0D,
        0x1C, 0x1D, 0x1E, 0x1F, 0x20,
        0x85, 0xA0, 0x1680,
        0x2000, 0x2005, 0x200A, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000,
    )
)

#: Identifiers whose verdict every reading of the grammar must agree on.
IDENTIFIER_ROWS = (
    ("ordinary", "svc"),
    ("comma", "a,b"),
    ("workflow command", "::error::pwn"),
    ("empty", ""),
    ("at the cap", "x" * 16384),
    ("one over the cap", "x" * 16385),
    ("interior space", "a b"),
    ("zero width space", "a\u200bb"),
    *(
        (f"leading U+{ord(character):04X}", character + "svc")
        for character in STRIP_CODEPOINTS
    ),
    *(
        (f"trailing U+{ord(character):04X}", "svc" + character)
        for character in STRIP_CODEPOINTS
    ),
    *(
        (f"interior U+{ord(character):04X}", "sv" + character + "c")
        for character in STRIP_CODEPOINTS
    ),
)

_ROOT_KEYS = ("project", "components", "defaults", "slices", "surprise")


class _WithoutSchemaExtras:
    """Make ``import jsonschema`` raise inside ``_schema_engine_errors``.

    Setting the module entry to ``None`` is the documented way to make an
    import fail without touching the interpreter's import machinery, and it
    restores exactly what was there before.
    """

    def __enter__(self) -> "_WithoutSchemaExtras":
        self._saved = {
            name: sys.modules.get(name, KeyError)
            for name in ("jsonschema", "referencing")
        }
        for name in ("jsonschema", "referencing"):
            sys.modules[name] = None  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: object) -> None:
        for name, value in self._saved.items():
            if value is KeyError:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value  # type: ignore[assignment]


class SchemaEngineParityTests(unittest.TestCase):
    """OBL-GIT-SOURCE-069: one verdict, two engines, one grammar."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = Scenario()
        cls.scene.file("svc/a.txt", "x\n")
        cls.scene.file("other/b.txt", "x\n")
        cls.scene.commit("base")
        cls.root = cls.scene.root

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    def _validate(self, document: dict) -> List[str]:
        # `index` avoids the working-tree tracking snapshot, which costs a Git
        # subprocess per call and validates nothing these documents exercise.
        return validate_config(document, self.root, source="index")

    def _both_engines(self, document: dict) -> Tuple[List[str], List[str]]:
        with_extras = self._validate(document)
        with _WithoutSchemaExtras():
            without_extras = self._validate(document)
        return with_extras, without_extras

    @staticmethod
    def _named(name: str) -> dict:
        return {
            "project": "p",
            "components": {
                name: {"path": "svc", "boundary": {"provider": "leaf", "paths": []}}
            },
        }

    # ---- premises ---------------------------------------------------------

    def test_the_extras_are_installed_and_can_be_taken_away(self):
        """Premise: without this the parity assertions compare one engine to
        itself and can never fail."""
        import jsonschema  # noqa: F401

        document = {"project": "p", "components": {}, "surprise": 1}
        with_extras, without_extras = self._both_engines(document)
        self.assertTrue(
            any(
                line.startswith("Schema validation error at ")
                for line in with_extras
            ),
            with_extras,
        )
        self.assertEqual(
            [
                line
                for line in without_extras
                if line.startswith("Schema validation error at ")
            ],
            [],
        )
        self.assertIn("jsonschema", sys.modules)

    def test_the_grammar_table_contains_both_verdicts(self):
        """Premise: a table every reading rejects would prove nothing."""
        accepted = [
            name
            for _label, name in IDENTIFIER_ROWS
            if component_identifier_problem(name) is None
        ]
        self.assertEqual(len(accepted), 26)
        self.assertLess(len(accepted), len(IDENTIFIER_ROWS))

    # ---- the grammar, read three ways -------------------------------------

    def test_the_component_grammar_agrees_across_every_reading(self):
        """The packaged schema, the shared constant and the dependency-free
        validator are three copies of one grammar; the pipeline decision is
        the fourth reading that users actually meet."""
        import jsonschema
        from referencing import Registry

        schema = _load_config_schema(self.root)
        self.assertIsNotNone(schema)
        subschema = schema["$defs"]["componentIdentifier"]
        engine = jsonschema.Draft202012Validator(subschema, registry=Registry())
        constant = re.compile(COMPONENT_IDENTIFIER_PATTERN)
        for label, name in IDENTIFIER_ROWS:
            with self.subTest(identifier=label):
                by_schema = not list(engine.iter_errors(name))
                by_constant = (
                    constant.fullmatch(name) is not None
                    and 1 <= len(name) <= 16384
                )
                by_validator = component_identifier_problem(name) is None
                by_pipeline = not self._validate(self._named(name))
                self.assertEqual(by_schema, by_constant)
                self.assertEqual(by_schema, by_validator)
                self.assertEqual(by_schema, by_pipeline)

    def test_the_grammar_verdict_does_not_depend_on_the_extras(self):
        for label, name in IDENTIFIER_ROWS:
            with self.subTest(identifier=label):
                with_extras, without_extras = self._both_engines(self._named(name))
                self.assertEqual(bool(with_extras), bool(without_extras))

    # ---- the document-level property --------------------------------------

    @settings(
        max_examples=120,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        keys=st.lists(st.sampled_from(_ROOT_KEYS), unique=True, max_size=5),
        project=st.one_of(
            st.text(max_size=5), st.integers(), st.none(), st.booleans()
        ),
        name=st.text(max_size=6),
        path=st.one_of(st.text(max_size=6), st.integers(), st.none()),
        provider=st.sampled_from(("leaf", "path-hash", "nope", "custom.x")),
        paths=st.one_of(
            st.lists(st.text(max_size=4), max_size=2), st.text(max_size=4), st.none()
        ),
    )
    def test_the_accept_reject_decision_is_the_same_without_the_extras(
        self, keys, project, name, path, provider, paths
    ):
        document: dict = {}
        for key in keys:
            if key == "project":
                document["project"] = project
            elif key == "components":
                document["components"] = {
                    name: {
                        "path": path,
                        "boundary": {"provider": provider, "paths": paths},
                    }
                }
            elif key == "defaults":
                document["defaults"] = {"compat_mode": provider}
            elif key == "slices":
                document["slices"] = {"s": {"mode": "exact", "components": [name]}}
            else:
                document["surprise"] = 1
        with_extras, without_extras = self._both_engines(document)
        self.assertEqual(bool(with_extras), bool(without_extras), document)
        self.assertEqual(
            [line for line in without_extras if line not in with_extras],
            [],
            document,
        )
        self.assertEqual(
            [
                line
                for line in with_extras
                if line not in without_extras
                and not line.startswith("Schema validation error at ")
            ],
            [],
            document,
        )

    def test_the_extras_add_only_schema_engine_lines(self):
        """The pin beside the divergence: the difference is additive and every
        added line is recognisable, so no field goes unreported either way."""
        document = {
            "project": "",
            "components": {
                "a,b": {"path": "svc", "boundary": {"provider": "leaf", "paths": []}},
                "ok": {"path": 5, "boundary": {"provider": "nope", "paths": "x"}},
            },
            "unknown_root": 1,
        }
        with_extras, without_extras = self._both_engines(document)
        self.assertEqual(len(without_extras), 6)
        self.assertEqual(len(with_extras), 11)
        extra = [line for line in with_extras if line not in without_extras]
        self.assertEqual(len(extra), 5)
        for line in extra:
            self.assertTrue(
                line.startswith("Schema validation error at "), line
            )
        self.assertEqual(
            [line for line in without_extras if line not in with_extras], []
        )

    # ---- the packaged schema outranks the checkout ------------------------

    def test_a_permissive_repository_schema_cannot_weaken_validation(self):
        permissive = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
        }
        document = self._named("a,b")
        local = self.root / "boundary.config.schema.json"
        try:
            local.write_text(json.dumps(permissive), encoding="utf-8")
            loaded = _load_config_schema(self.root)
            self.assertNotEqual(loaded, permissive)
            self.assertEqual(
                loaded["$defs"]["componentIdentifier"]["maxLength"], 16384
            )
            self.assertTrue(self._validate(document))

            def _unavailable(*args: object, **kwargs: object) -> str:
                raise FileNotFoundError("packaged schema unavailable")

            # Premise: the repository copy is reachable, so the assertion
            # above is about precedence and not about a dead code path.
            with mock.patch.object(
                config_module.resources, "read_text", _unavailable
            ):
                self.assertEqual(_load_config_schema(self.root), permissive)
                fallback = self._validate(document)
            self.assertTrue(fallback)
            self.assertEqual(
                [
                    line
                    for line in fallback
                    if line.startswith("Schema validation error at ")
                ],
                [],
            )
        finally:
            local.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-070 - head, index and working tree agree on a clean tree
# ---------------------------------------------------------------------------

#: File bodies whose Git storage or line endings could plausibly differ
#: between what HEAD holds, what the index holds and what is on disk.
CONTENT_KINDS = {
    "lf text": b"first\nsecond\n",
    "crlf text": b"first\r\nsecond\r\n",
    "lone cr": b"first\rsecond\r",
    "mixed endings": b"a\r\nb\nc\rd",
    "binary with nul": b"\x00\x01\x02\xff\xfe",
    "zero byte": b"",
    "utf-8 bom": b"\xef\xbb\xbftext\n",
    "no trailing newline": b"tail",
}

#: Path shapes with the same question about them.
NAME_KINDS = {
    "plain": "plain.txt",
    "dotfile": ".hidden",
    "deep": "/".join(f"level{index}" for index in range(12)) + "/deep.txt",
    "non ascii": "\u00e9\u4e2d\u6587-\u0161.txt",
    "spaces": "name with spaces.txt",
    "hash": "na#me.txt",
    "no extension": "Makefile",
}


class SourceModeTreeParityTests(unittest.TestCase):
    """OBL-GIT-SOURCE-070: a clean tree hashes the same from every source."""

    def _digests(self, scene: Scenario, path: str = "svc") -> Dict[str, str]:
        return {
            mode: source_tree_digest(scene.root, path, mode)
            for mode in SOURCE_MODES
        }

    def test_a_single_changed_byte_changes_the_digest(self):
        """Premise: agreement between three modes means something only if the
        digest reacts to content at all."""
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="path-hash")
            scene.file("svc/a.txt", "content\n")
            scene.commit("base")
            before = source_tree_digest(scene.root, "svc", "head")
            scene.file("svc/a.txt", "contens\n")
            scene.commit("edit")
            self.assertIsNotNone(before)
            self.assertNotEqual(source_tree_digest(scene.root, "svc", "head"), before)

    def test_a_dirty_tree_makes_the_modes_disagree(self):
        """Premise: the three modes are genuinely three readings, so a clean
        tree agreeing is a fact about the tree and not about the API."""
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="path-hash")
            scene.file("svc/a.txt", "content\n")
            scene.commit("base")
            scene.file("svc/a.txt", "uncommitted\n")
            digests = self._digests(scene)
            self.assertEqual(digests["head"], digests["index"])
            self.assertNotEqual(digests["head"], digests["working-tree"])

    @settings(
        max_examples=12,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        entries=st.lists(
            st.tuples(
                st.sampled_from(sorted(NAME_KINDS)),
                st.sampled_from(sorted(CONTENT_KINDS)),
            ),
            min_size=1,
            max_size=8,
        )
    )
    def test_a_generated_clean_tree_hashes_the_same_from_every_source(
        self, entries
    ):
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="path-hash")
            for index, (name_kind, content_kind) in enumerate(entries):
                target = scene.root / "svc" / f"{index}-{NAME_KINDS[name_kind]}"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(CONTENT_KINDS[content_kind])
            scene.commit("generated tree")
            status = scene.git("status", "--porcelain")
            self.assertEqual(status, "", f"tree is not clean:\n{status}")
            digests = self._digests(scene)
            self.assertIsNotNone(digests["head"])
            self.assertEqual(len(set(digests.values())), 1, digests)

    def test_every_file_kind_together_agrees_and_the_lock_bytes_match(self):
        """The derived facets, not only the tree digest: a source-mode leak
        anywhere in the document would show here."""
        with Scenario() as scene:
            scene.component(
                "svc", path="svc", provider="path-hash", boundary=["**/*"]
            )
            index = 0
            for name in NAME_KINDS.values():
                for content in CONTENT_KINDS.values():
                    target = scene.root / "svc" / f"{index}-{name}"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                    index += 1
            scene.commit("every kind")
            self.assertEqual(scene.git("status", "--porcelain"), "")
            self.assertEqual(index, len(NAME_KINDS) * len(CONTENT_KINDS))
            self.assertEqual(len(set(self._digests(scene).values())), 1)
            rendered = {
                mode: dump_lockfile(scene.generate(source=mode)).encode("utf-8")
                for mode in SOURCE_MODES
            }
            self.assertEqual(len(set(rendered.values())), 1)

    def test_an_executable_blob_agrees_across_the_modes(self):
        """The mode lives in the index here, because the development host
        cannot store an execute bit; `core.filemode` is false, so Git reports
        the tree clean and all three readings must still match."""
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="path-hash")
            scene.file("svc/tool.sh", "#!/bin/sh\necho hi\n")
            scene.file("svc/plain.txt", "x\n")
            scene.commit("base")
            scene.git("update-index", "--chmod=+x", "svc/tool.sh")
            scene.commit_index("executable")
            self.assertEqual(scene.git("status", "--porcelain"), "")
            self.assertIn(
                "100755 ", scene.git("ls-files", "--stage", "svc/tool.sh")
            )
            self.assertEqual(len(set(self._digests(scene).values())), 1)

    @requires_symlinks
    def test_a_symlink_agrees_across_the_modes(self):
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="path-hash")
            scene.file("svc/target.txt", "x\n")
            scene.symlink("svc/link.txt", "target.txt")
            scene.commit("base")
            self.assertEqual(scene.git("status", "--porcelain"), "")
            self.assertEqual(len(set(self._digests(scene).values())), 1)

    def test_a_surrogate_filename_cannot_enter_a_repository_on_this_host(self):
        """Why the surrogate-escaped filename case stays a recorded gap.

        The register says such a name cannot be created here. That is not
        quite right: Windows file names are UTF-16 and Python's filesystem
        encoding is utf-8/surrogatepass, so the file is created. Git is where
        it stops. Git for Windows converts the wide name to UTF-8 by replacing
        the unpaired surrogate with U+FFFD, then fails to open the name it
        invented, so the path can never reach an index, a tree or a digest on
        this host. Pinning the failure keeps the gap honest: when this test
        starts failing, the real parity test becomes writable.
        """
        if os.name != "nt":
            self.skipTest("this limitation is specific to the Windows host")
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="path-hash")
            (scene.root / "svc").mkdir(parents=True, exist_ok=True)
            target = scene.root / "svc" / "broken-\udcff.txt"
            target.write_bytes(b"payload\n")
            self.assertTrue(target.exists(), "Python did create the file")
            scene.write_config()
            outcome = subprocess.run(
                ["git", "add", "--all"],
                cwd=scene.root,
                capture_output=True,
            )
            self.assertEqual(outcome.returncode, 128)
            message = outcome.stderr.decode("utf-8", "replace")
            self.assertIn("unable to index file", message)
            self.assertIn("�", message)


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
