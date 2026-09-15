"""Two refusal surfaces that decide whether a stale identity can be laundered.

`migrate-lock` is the one command allowed to rewrite a committed lock, and the
whole of its safety is in what it declines to do. A v1 or v2 lock, or a v3 lock
carrying the older semantic-config contract, holds digests computed under a
different hash frame; relabelling one would produce a document that claims the
current contract while its fingerprints mean something else, and `verify` would
then pass against them. So the interesting assertions here are all negative:
the command must exit with the usage code, leave the file byte-identical, and
say the same thing about how to recover. Three of the five refusals do name
`boundver generate`; the two that reject the schema field itself do not, and
that is pinned below as a divergence rather than smoothed over. The other half
of the same command is the `detail` line, the only record of what a migration
actually changed. Driving all four repairable shapes - with a component map
present and `generated_at` and `slices` independently present or absent -
shows that two of the remaining clauses are unreachable: the
schema comparison can never differ, because the only schema the guards let
through is the one the copy assigns, and the fallback behind it therefore
cannot fire either. Both dead phrases are looked up in the function's own
source first, so this is an absence in the product rather than an absence in
the file being read.

The version read layer is the other subject. `extract_version` now normalizes
both the declared file and component root before either the disk fallback or
an injected reader sees a path. The tests prove traversal and NTFS stream
spellings are refused without delegating containment to the reader. The
remaining encoding and field-path obligations are separate:
a UTF-8 BOM makes the same release read `1.2.3` from `chart.yaml` and nothing
at all from `package.json`, because PyYAML tolerates a leading U+FEFF in a str
while `json.loads` and `tomllib.loads` do not; and `field` is split on `.` with
no escaping, so `a..b` and a bare `.` pass configuration validation untouched
and then dissolve into "Configured version source did not produce a version" at
generate time, while a key that genuinely contains a dot cannot be addressed at
all and is silently outvoted by the nesting of the same name.

Two of the tables here would otherwise be claims rather than checks. The
four-cell detail table says it covers everything `_cmd_migrate_lock` can
print, and the four-extension document table says it covers every extension
`_extract_field_from_bytes` dispatches on; both are hand-written, and a fifth
clause or a fifth extension added to the product would leave them quietly
incomplete. So each is now checked against the source it describes: the clause
expressions are read back out of the function with a line-anchored regular
expression and compared as a list, and the `endswith` literals are read out of
the reader and compared as a set. Adding a clause to the command or an arm to
the dispatch makes this file red until a row is added here.

The negative assertions carry the same burden. An absence is evidence only when
the run that produced it reached the output being searched, so each one first
requires the exit code and a positive marker from the branch under test - the
rendered "Would normalize" or "Lockfile is already normalized" line for the
dead clauses, "Lock action: normalize" for `--explain`, the prospective JSON on
stdout for a dry run - and only then asks what is missing. That anchoring is
not theoretical: pointing every `migrate-lock` run at a lockfile that does not
exist, so the command exited at load time and printed nothing at all, left
three of these tests green.

Two details of the fixture are worth naming. Diagnostics containing an em dash
cannot be asserted through `run_cli`, because this host decodes the child's
stderr as cp1252 and the character does not survive the trip; the exact text
including that character is pinned at library level instead, and the CLI
assertions use ASCII substrings observed from the same runs. And the
colliding-key document had to be expressible in all three formats at once -
TOML accepts `"a.b" = ...` alongside `[a]` in one file, and YAML and JSON
accept the equivalent - so that "which node does this path select" is one
question asked three ways rather than three separate questions.

Covers OBL-LOCKFILE-022, OBL-LOCKFILE-038, OBL-LOCKFILE-039, OBL-CONFIG-040,
OBL-CONFIG-041 and OBL-CONFIG-042.
"""

from __future__ import annotations

import codecs
import inspect
import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from boundver import core
from boundver._config import validate_config
from boundver._lockfile import (
    KNOWN_SCHEMAS,
    LOCKFILE_SCHEMA,
    SEMANTIC_CONFIG_VERSION,
    MigrationError,
    _SourceAccessor,
    migrate_lockfile,
)
from boundver.core import EXIT_OK, EXIT_USAGE
from boundver.versions import _extract_field_from_bytes, extract_version

from tests._parity import run_cli, run_cli_in_process
from tests._scenarios import SOURCE_MODES, Scenario

try:  # PyYAML is an optional extra; the .yaml column of two tables needs it.
    import yaml
except ImportError:  # pragma: no cover - exercised on hosts without the extra
    yaml = None

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 ships tomli instead
    import tomli as tomllib

requires_yaml = unittest.skipUnless(yaml is not None, "PyYAML is not installed")

#: Sentinel for "this key was absent", so a key holding None is not mistaken
#: for a key that is not there.
_MISSING = object()


#: Every hash contract `migrate_lockfile` must refuse, paired with a phrase
#: observed in the message it actually raises. v3 is always refused because it
#: predates the complete semantic-config-v3 declaration set; its embedded
#: config-contract spelling cannot make that lock mechanically migratable.
UNMIGRATABLE_LOCKS: Dict[str, Tuple[Dict[str, Any], str]] = {
    "no schema field": (
        {"project": "scenario"},
        "Lockfile has no 'schema' field",
    ),
    "schema is not a string": (
        {"schema": 3},
        "Unknown lockfile schema 3. Supported: ",
    ),
    "schema is unknown": (
        {"schema": "boundary-lock/v9"},
        "Unknown lockfile schema 'boundary-lock/v9'. Supported: ",
    ),
    "hash contract v1": (
        {"schema": "boundary-lock/v1"},
        "boundary-lock/v1 does not bind every file's Git mode/type",
    ),
    "hash contract v2": (
        {"schema": "boundary-lock/v2"},
        "boundary-lock/v2 does not bind every file's Git mode/type",
    ),
    "v3 carrying semantic-config/v1": (
        {
            "schema": "boundary-lock/v3",
            "config_contract": "boundver-semantic-config/v1",
        },
        "does not bind the complete boundver-semantic-config/v3 declaration set",
    ),
    "v3 carrying semantic-config/v2": (
        {
            "schema": "boundary-lock/v3",
            "config_contract": "boundver-semantic-config/v2",
        },
        "does not bind the complete boundver-semantic-config/v3 declaration set",
    ),
    "v3 with no config_contract": (
        {"schema": "boundary-lock/v3"},
        "does not bind the complete boundver-semantic-config/v3 declaration set",
    ),
    "v3 with a non-string config_contract": (
        {"schema": "boundary-lock/v3", "config_contract": 2},
        "does not bind the complete boundver-semantic-config/v3 declaration set",
    ),
    "current schema with a stale config_contract": (
        {
            "schema": LOCKFILE_SCHEMA,
            "config_contract": "boundver-semantic-config/v2",
            "components": {},
        },
        "uses semantic configuration contract",
    ),
    "current schema with no config_contract": (
        {"schema": LOCKFILE_SCHEMA, "components": {}},
        "uses semantic configuration contract",
    ),
}

#: Every refusal names the same repository-content recovery command.
REFUSALS_NAMING_GENERATE = frozenset(UNMIGRATABLE_LOCKS)

#: The four lockfiles `migrate_lockfile` accepts, keyed by whether
#: `generated_at`, `components` and `slices` are present, mapped to the exact
#: `detail` the CLI prints for each. ``None`` means the lock was already
#: normalized, so no detail is rendered at all.
NORMALIZATION_DETAILS: Dict[Tuple[bool, bool, bool], Optional[str]] = {
    (False, True, False): "added missing slices map",
    (False, True, True): None,
    (True, True, False): (
        "removed legacy generated_at metadata, added missing slices map"
    ),
    (True, True, True): "removed legacy generated_at metadata",
}

#: The two clauses `_cmd_migrate_lock` can render that no accepted lockfile can
#: reach. They are looked up in the function's source before their absence is
#: asserted, so a deletion cannot masquerade as unreachability.
DEAD_DETAIL_CLAUSES = ("set schema to ", "normalized supported lock metadata")

#: Every expression `_cmd_migrate_lock` appends to its clause list, quoted
#: exactly as the source spells it and in source order. This is what makes the
#: table above a checker rather than a claim: a fifth clause added to the
#: command changes this list, and the test that reads it back out of the source
#: goes red until the new clause is either driven by NORMALIZATION_DETAILS or
#: declared dead.
DETAIL_CLAUSE_EXPRESSIONS = (
    '"removed legacy generated_at metadata"',
    '"added missing slices map"',
    "f\"set schema to {migrated.get('schema')}\"",
)

#: The line that turns that list into the printed detail. The string on its
#: right is the second clause no accepted lockfile can reach.
DETAIL_JOIN_LINE = (
    'detail = ", ".join(normalized) or "normalized supported lock metadata"'
)

#: One `normalized.append(...)` statement, anchored to its own line: the schema
#: clause carries a `)` inside its f-string, so a lazy match to the first
#: closing parenthesis would truncate it.
CLAUSE_STATEMENT = re.compile(r"^\s*normalized\.append\((.*)\)\s*$", re.MULTILINE)

#: One `file_rel.endswith('...')` arm of the extension dispatch.
DISPATCH_ARM = re.compile(r"endswith\(\s*['\"]([^'\"]+)['\"]\s*\)")

#: The two keys an accepted migration is allowed to change. `schema` is
#: deliberately absent: it is assigned unconditionally, but only to the value
#: every accepted lock already carries.
MIGRATABLE_KEYS = frozenset({"generated_at", "slices"})

#: What the shipped `_SourceAccessor` says when handed a repository-relative
#: path that escapes the repository. Not one of these is a containment check:
#: two report an absent tree entry and the third an untracked path, which is
#: why the guarantee is described here as holding by luck.
ACCESSOR_REFUSALS = {
    "head": "Path is absent from captured head tree: ../outside/secret.json",
    "index": "Path is absent from captured index tree: ../outside/secret.json",
    "working-tree": (
        "Version source is not tracked in the captured index: "
        "../outside/secret.json"
    ),
}

#: One logical document - a single field `version` holding "1.2.3" - written in
#: every extension `_extract_field_from_bytes` dispatches on.
VERSION_DOCUMENTS = {
    ".json": '{"version": "1.2.3"}',
    ".toml": 'version = "1.2.3"',
    ".yaml": 'version: "1.2.3"',
    ".yml": 'version: "1.2.3"',
}

#: Four byte-level spellings of whatever text they are given. `str.encode`
#: with "utf-16" emits a BOM plus little-endian code units on this host, which
#: is what a Windows editor saving as "Unicode" writes.
ENCODINGS = {
    "utf-8": lambda text: text.encode("utf-8"),
    "utf-8 with BOM": lambda text: codecs.BOM_UTF8 + text.encode("utf-8"),
    "utf-16le with BOM": lambda text: text.encode("utf-16"),
    "utf-8 with one invalid byte": lambda text: b"\xff" + text.encode("utf-8"),
}

#: One document written three ways, carrying both a literal key "a.b" and the
#: nesting a -> b. TOML accepts both spellings in one file, which is what makes
#: the collision expressible in every format rather than only in JSON.
COLLIDING_DOCUMENTS = {
    ".json": json.dumps({"version": "1.0.0", "a": {"b": "2.0.0"}, "a.b": "9.9.9"}),
    ".toml": 'version = "1.0.0"\n"a.b" = "9.9.9"\n\n[a]\nb = "2.0.0"\n',
    ".yaml": 'version: "1.0.0"\n"a.b": "9.9.9"\na:\n  b: "2.0.0"\n',
}

#: The same three renderings carrying only the literal dotted key, so nothing
#: else can be what answers a lookup of "a.b".
LITERAL_ONLY_DOCUMENTS = {
    ".json": json.dumps({"a.b": "9.9.9"}),
    ".toml": '"a.b" = "9.9.9"\n',
    ".yaml": '"a.b": "9.9.9"\n',
}

#: The value both documents above agree on for the literal dotted key, which no
#: field path can select.
UNREACHABLE_VALUE = "9.9.9"

#: Field paths against COLLIDING_DOCUMENTS and the value each selects today,
#: identically in all three formats. "a.b" reaches the nesting, never "9.9.9",
#: and "a" reaches a mapping, which does not stringify into a version.
FIELD_PATH_VERDICTS = {
    "version": "1.0.0",
    "a.b": "2.0.0",
    "a..b": None,
    "version.": None,
    ".": None,
    "a": None,
}

#: Field paths that survive configuration validation untouched but resolve to
#: nothing, and so reach generate as a bare missing version.
DEGENERATE_FIELD_PATHS = ("a..b", "version.", ".")

#: What generate says when a version source resolves to nothing, whatever the
#: reason. Both the encoding obligation and the field-path obligation ask for
#: something more specific than this.
GENERIC_NO_VERSION = "Configured version source did not produce a version"


def _repository() -> Scenario:
    """A committed repository with one leaf component and a config on disk."""
    scene = Scenario()
    scene.component("svc", path="svc", provider="leaf")
    scene.file("svc/main.py", "x\n")
    scene.commit()
    return scene


def _current_lock(**extra: Any) -> Dict[str, Any]:
    """A lockfile `migrate_lockfile` accepts and, by default, leaves alone."""
    lock = {
        "schema": LOCKFILE_SCHEMA,
        "config_contract": SEMANTIC_CONFIG_VERSION,
        "project": "scenario",
        "components": {},
        "slices": {},
    }
    lock.update(extra)
    return lock


def _lock_with(generated_at: bool, components: bool, slices: bool) -> Dict[str, Any]:
    """One point of the eight-cell space of acceptable lockfiles."""
    lock: Dict[str, Any] = {
        "schema": LOCKFILE_SCHEMA,
        "config_contract": SEMANTIC_CONFIG_VERSION,
        "project": "scenario",
    }
    if generated_at:
        lock["generated_at"] = "2020-01-01T00:00:00Z"
    if components:
        lock["components"] = {}
    if slices:
        lock["slices"] = {}
    return lock


def _write_lock(scene: Scenario, lock: Dict[str, Any]) -> Path:
    path = scene.root / "boundary.lock.json"
    path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    return path


class _ReaderSpy:
    """A `read_file_fn` that records every path it is handed."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.calls: List[str] = []

    def __call__(self, repo_rel: str) -> bytes:
        self.calls.append(repo_rel)
        return (self.base / repo_rel).read_bytes()


class MigrateLockRefusalTests(unittest.TestCase):
    """OBL-LOCKFILE-022: an unmigratable contract is refused, not relabelled."""

    def test_a_current_lock_is_accepted_so_the_refusals_are_refusals(self):
        """The premise: this function does not simply raise on everything."""
        migrated = migrate_lockfile(_current_lock())
        self.assertEqual(migrated["schema"], LOCKFILE_SCHEMA)
        self.assertEqual(migrated["config_contract"], SEMANTIC_CONFIG_VERSION)

    def test_every_unmigratable_contract_raises_migration_error(self):
        for label, (lock, phrase) in UNMIGRATABLE_LOCKS.items():
            with self.subTest(lock=label):
                with self.assertRaises(MigrationError) as caught:
                    migrate_lockfile(dict(lock))
                self.assertIn(phrase, str(caught.exception))

    def test_a_refusal_never_mutates_the_lockfile_it_was_handed(self):
        for label, (lock, _phrase) in UNMIGRATABLE_LOCKS.items():
            with self.subTest(lock=label):
                given = dict(lock)
                before = json.dumps(given, sort_keys=True)
                with self.assertRaises(MigrationError):
                    migrate_lockfile(given)
                self.assertEqual(json.dumps(given, sort_keys=True), before)

    def test_migrate_lock_exits_usage_and_writes_nothing_for_each_of_them(self):
        with _repository() as scene:
            for label, (lock, phrase) in UNMIGRATABLE_LOCKS.items():
                with self.subTest(lock=label):
                    path = _write_lock(scene, lock)
                    before = path.read_bytes()
                    result = run_cli(scene.root, "migrate-lock")
                    self.assertEqual(result.returncode, EXIT_USAGE, result.stderr)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(phrase, result.stderr)
                    self.assertTrue(result.stderr.startswith("error: "), result.stderr)

    def test_migrate_lock_does_rewrite_a_lock_it_can_normalize(self):
        """The premise for "writes nothing": the writer is reachable here."""
        with _repository() as scene:
            path = _write_lock(scene, _current_lock(generated_at="2020-01-01T00:00:00Z"))
            before = path.read_bytes()
            result = run_cli(scene.root, "migrate-lock")
            self.assertEqual(result.returncode, EXIT_OK, result.stderr)
            self.assertNotEqual(path.read_bytes(), before)
            self.assertNotIn("generated_at", path.read_text(encoding="utf-8"))
            self.assertIn("Normalized ", result.stdout)

    def test_every_refusal_names_the_generate_command(self):
        """Every refusal gives the repository-content recovery command."""
        silent = []
        for label, (lock, _phrase) in UNMIGRATABLE_LOCKS.items():
            try:
                migrate_lockfile(dict(lock))
            except MigrationError as exc:
                if "boundver generate" not in str(exc):
                    silent.append(label)
        self.assertEqual(silent, [])

    def test_the_content_refusals_do_name_the_generate_command(self):
        """Pinning the half of the conjunct that holds."""
        for label in sorted(REFUSALS_NAMING_GENERATE):
            with self.subTest(lock=label):
                lock, _phrase = UNMIGRATABLE_LOCKS[label]
                with self.assertRaises(MigrationError) as caught:
                    migrate_lockfile(dict(lock))
                self.assertIn("`boundver generate`", str(caught.exception))

    def test_the_two_schema_refusals_carry_actionable_messages(self):
        with self.assertRaises(MigrationError) as missing:
            migrate_lockfile({"project": "scenario"})
        self.assertEqual(
            str(missing.exception),
            "Lockfile has no 'schema' field — cannot determine version to "
            "migrate from. Run `boundver generate` to create a "
            f"{LOCKFILE_SCHEMA} lockfile.",
        )
        with self.assertRaises(MigrationError) as unknown:
            migrate_lockfile({"schema": "boundary-lock/v9"})
        self.assertEqual(
            str(unknown.exception),
            "Unknown lockfile schema 'boundary-lock/v9'. Supported: "
            + ", ".join(sorted(KNOWN_SCHEMAS))
            + ". Upgrade boundver if needed, then run `boundver generate` to "
            f"create a {LOCKFILE_SCHEMA} lockfile.",
        )

    def test_the_labels_that_do_and_do_not_name_generate_partition_the_table(self):
        """So a row added above cannot sit in neither half unnoticed."""
        self.assertTrue(REFUSALS_NAMING_GENERATE.issubset(UNMIGRATABLE_LOCKS))
        self.assertEqual(set(UNMIGRATABLE_LOCKS), REFUSALS_NAMING_GENERATE)


class MigrateLockDetailTests(unittest.TestCase):
    """OBL-LOCKFILE-038: the detail line names what was actually done."""

    def test_the_four_acceptable_lockfiles_report_exactly_their_clauses(self):
        self.assertEqual(
            set(NORMALIZATION_DETAILS),
            {
                (generated_at, True, slices)
                for generated_at in (False, True)
                for slices in (False, True)
            },
            "the table must cover every repairable presence shape",
        )
        with _repository() as scene:
            for flags, detail in NORMALIZATION_DETAILS.items():
                with self.subTest(present=flags):
                    _write_lock(scene, _lock_with(*flags))
                    result = run_cli(scene.root, "migrate-lock", "--dry-run")
                    self.assertEqual(result.returncode, EXIT_OK, result.stderr)
                    line = result.stderr.strip().splitlines()[-1]
                    if detail is None:
                        self.assertTrue(
                            line.startswith("Lockfile is already normalized: "), line
                        )
                        self.assertTrue(
                            line.endswith("; no changes would be written."), line
                        )
                    else:
                        self.assertTrue(line.startswith("Would normalize "), line)
                        self.assertTrue(line.endswith(f": {detail}."), line)

    def test_a_dry_run_leaves_every_one_of_those_lockfiles_untouched(self):
        with _repository() as scene:
            for flags in NORMALIZATION_DETAILS:
                with self.subTest(present=flags):
                    path = _write_lock(scene, _lock_with(*flags))
                    before = path.read_bytes()
                    run_cli(scene.root, "migrate-lock", "--dry-run")
                    self.assertEqual(path.read_bytes(), before)

    def test_both_dead_clauses_are_present_in_the_function_under_test(self):
        """The premise: their absence below is the product's, not this file's."""
        source = inspect.getsource(core._cmd_migrate_lock)
        for clause in DEAD_DETAIL_CLAUSES:
            with self.subTest(clause=clause):
                self.assertIn(clause, source)

    def test_no_acceptable_lockfile_can_render_either_dead_clause(self):
        with _repository() as scene:
            for flags in NORMALIZATION_DETAILS:
                with self.subTest(present=flags):
                    _write_lock(scene, _lock_with(*flags))
                    result = run_cli(scene.root, "migrate-lock", "--dry-run")
                    printed = result.stdout + result.stderr
                    for clause in DEAD_DETAIL_CLAUSES:
                        self.assertNotIn(clause, printed)

    def test_only_the_two_live_keys_can_differ_after_an_accepted_migration(self):
        """The structural reason both clauses are dead, read off the documents.

        `_cmd_migrate_lock` renders "set schema to ..." when the schema moved
        and falls back to "normalized supported lock metadata" when a lock
        changed for none of the listed reasons. Neither can happen while
        every accepted lock differs from its migration only in these keys.
        """
        for flags in NORMALIZATION_DETAILS:
            with self.subTest(present=flags):
                lock = _lock_with(*flags)
                migrated = migrate_lockfile(dict(lock))
                differing = {
                    key
                    for key in set(lock) | set(migrated)
                    if lock.get(key, _MISSING) is not migrated.get(key, _MISSING)
                    and lock.get(key, _MISSING) != migrated.get(key, _MISSING)
                }
                self.assertLessEqual(differing, MIGRATABLE_KEYS)

    def test_the_only_schema_migrate_lockfile_accepts_is_the_one_it_assigns(self):
        """Enumerated from the schema set the module itself publishes."""
        candidates = list(KNOWN_SCHEMAS) + ["boundary-lock/v9", None, 3, ["v3"]]
        accepted = []
        for schema in candidates:
            lock = {
                "schema": schema,
                "config_contract": SEMANTIC_CONFIG_VERSION,
                "components": {},
            }
            try:
                migrated = migrate_lockfile(lock)
            except MigrationError:
                continue
            accepted.append(schema)
            self.assertEqual(migrated["schema"], schema)
            self.assertEqual(migrated["schema"], LOCKFILE_SCHEMA)
        self.assertEqual(accepted, [LOCKFILE_SCHEMA])


class ExplainIsReadOnlyTests(unittest.TestCase):
    """OBL-LOCKFILE-039: --explain inspects, and --dry-run cannot join it."""

    def _spy_on_the_writer(self):
        calls: List[Tuple[str, int]] = []
        real = core._write_text_atomic

        def spy(path, text):
            calls.append((str(path), len(text)))
            return real(path, text)

        return calls, spy

    def test_a_normalizable_lock_without_explain_does_call_the_atomic_writer(self):
        """The premise for the absence asserted next."""
        with _repository() as scene:
            path = _write_lock(scene, _current_lock(generated_at="2020-01-01T00:00:00Z"))
            calls, spy = self._spy_on_the_writer()
            with mock.patch.object(core, "_write_text_atomic", spy):
                result = run_cli_in_process(scene.root, "migrate-lock")
            self.assertEqual(result.returncode, EXIT_OK, result.stderr)
            self.assertEqual([call[0] for call in calls], [str(path)])

    def test_explain_on_a_normalizable_lock_never_reaches_the_writer(self):
        with _repository() as scene:
            path = _write_lock(scene, _current_lock(generated_at="2020-01-01T00:00:00Z"))
            before, stat_before = path.read_bytes(), path.stat()
            calls, spy = self._spy_on_the_writer()
            with mock.patch.object(core, "_write_text_atomic", spy):
                result = run_cli_in_process(scene.root, "migrate-lock", "--explain")
            stat_after = path.stat()
            self.assertEqual(result.returncode, EXIT_OK, result.stderr)
            self.assertEqual(calls, [])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(stat_after.st_mtime_ns, stat_before.st_mtime_ns)
            self.assertEqual(stat_after.st_ino, stat_before.st_ino)

    def test_that_lock_really_did_take_the_normalize_branch(self):
        """Otherwise the test above would be explaining a rejected lock.

        Every existing --explain test uses a lock whose action is "regenerate",
        which returns long before the write block for an unrelated reason. The
        branch this file exists to exercise is the one where `migrate_lockfile`
        succeeded and a migrated document is sitting in hand.
        """
        with _repository() as scene:
            _write_lock(scene, _current_lock(generated_at="2020-01-01T00:00:00Z"))
            text = run_cli(scene.root, "migrate-lock", "--explain")
            self.assertEqual(text.returncode, EXIT_OK, text.stderr)
            self.assertIn("Lock action: normalize", text.stdout)
            payload = json.loads(
                run_cli(
                    scene.root, "migrate-lock", "--explain", "--format", "json"
                ).stdout
            )
            self.assertEqual(payload["lock"]["action"], "normalize")
            self.assertEqual(payload["lock"]["source_schema"], LOCKFILE_SCHEMA)
            self.assertIsNone(payload["lock"]["reason"])

    def test_explain_says_nothing_about_writing(self):
        """The writing vocabulary is observed in MigrateLockDetailTests above."""
        with _repository() as scene:
            _write_lock(scene, _current_lock(generated_at="2020-01-01T00:00:00Z"))
            result = run_cli(scene.root, "migrate-lock", "--explain")
            printed = result.stdout + result.stderr
            for word in ("Normalized ", "Would normalize ", "written"):
                with self.subTest(word=word):
                    self.assertNotIn(word, printed)

    def test_each_mode_flag_is_accepted_on_its_own(self):
        """The premise for the mutual-exclusion refusal below."""
        with _repository() as scene:
            _write_lock(scene, _current_lock(generated_at="2020-01-01T00:00:00Z"))
            for flag in ("--dry-run", "--explain"):
                with self.subTest(flag=flag):
                    result = run_cli(scene.root, "migrate-lock", flag)
                    self.assertEqual(result.returncode, EXIT_OK, result.stderr)

    def test_explain_and_dry_run_are_refused_by_the_parser(self):
        with _repository() as scene:
            path = _write_lock(scene, _current_lock(generated_at="2020-01-01T00:00:00Z"))
            before = path.read_bytes()
            result = run_cli(scene.root, "migrate-lock", "--explain", "--dry-run")
            self.assertEqual(result.returncode, EXIT_USAGE)
            self.assertIn(
                "argument --dry-run: not allowed with argument --explain",
                result.stderr,
            )
            self.assertEqual(result.stdout, "")
            self.assertEqual(path.read_bytes(), before)


class VersionSourceContainmentTests(unittest.TestCase):
    """OBL-CONFIG-040: containment must not be delegated to the reader."""

    def _outside_and_in(self):
        """A repository with a sibling directory holding a file to steal."""
        directory = tempfile.TemporaryDirectory()
        base = Path(directory.name)
        (base / "outside").mkdir()
        (base / "outside" / "secret.json").write_text(
            '{"version": "9.9.9"}', encoding="utf-8"
        )
        repo = base / "repo"
        (repo / "svc").mkdir(parents=True)
        (repo / "svc" / "package.json").write_text(
            '{"version": "1.0.0"}', encoding="utf-8"
        )
        return directory, repo

    def test_the_reader_is_called_for_a_contained_component_path(self):
        """The premise: the spy records a call when a call is made."""
        directory, repo = self._outside_and_in()
        try:
            spy = _ReaderSpy(repo)
            found = extract_version(
                repo,
                "svc",
                {"file": "package.json", "field": "version"},
                read_file_fn=spy,
            )
            self.assertEqual(spy.calls, ["svc/package.json"])
            self.assertEqual(found, "1.0.0")
        finally:
            directory.cleanup()

    def test_a_traversing_declared_file_never_reaches_the_reader(self):
        """The premise: refusing before the call is something this code does.

        `version_source["file"]` goes through `_normalize_declared_path`, which
        rejects a '..' segment, and the function returns without touching the
        reader. That is the mechanism the next test says is missing on the
        other argument, so it has to be shown working on this one.
        """
        directory, repo = self._outside_and_in()
        try:
            spy = _ReaderSpy(repo)
            found = extract_version(
                repo,
                "svc",
                {"file": "../../outside/secret.json", "field": "version"},
                read_file_fn=spy,
            )
            self.assertEqual(spy.calls, [])
            self.assertIsNone(found)
        finally:
            directory.cleanup()

    def test_a_traversing_component_path_never_reaches_the_reader(self):
        """The public helper establishes containment before reader dispatch.

        A hostile component path is refused without delegating any part of the
        security decision to an injected reader.
        """
        directory, repo = self._outside_and_in()
        try:
            spy = _ReaderSpy(repo)
            extract_version(
                repo,
                "../outside",
                {"file": "secret.json", "field": "version"},
                read_file_fn=spy,
            )
            self.assertEqual(spy.calls, [])
        finally:
            directory.cleanup()

    def test_both_reader_branches_refuse_the_escape(self):
        directory, repo = self._outside_and_in()
        try:
            source = {"file": "secret.json", "field": "version"}
            self.assertIsNone(extract_version(repo, "../outside", source))
            spy = _ReaderSpy(repo)
            self.assertIsNone(
                extract_version(repo, "../outside", source, read_file_fn=spy)
            )
            self.assertEqual(spy.calls, [])
        finally:
            directory.cleanup()

    def test_the_disk_branch_refuses_on_containment_and_not_on_absence(self):
        """The premise for the line above: the escaping file does exist."""
        directory, repo = self._outside_and_in()
        try:
            escaping = repo / ".." / "outside" / "secret.json"
            self.assertTrue(escaping.exists())
            self.assertEqual(
                json.loads(escaping.read_text(encoding="utf-8"))["version"], "9.9.9"
            )
            self.assertEqual(
                extract_version(
                    repo, "svc", {"file": "package.json", "field": "version"}
                ),
                "1.0.0",
            )
        finally:
            directory.cleanup()

    def test_only_normalized_component_paths_reach_the_reader(self):
        accepted = {
            "plain": ("svc", "svc/x.json"),
            "trailing slash": ("svc/", "svc/x.json"),
            "repository root as dot": (".", "x.json"),
            "empty": ("", "x.json"),
        }
        for label, (component_path, expected) in accepted.items():
            with self.subTest(component_path=label):
                seen: List[str] = []

                def reader(repo_rel: str) -> bytes:
                    seen.append(repo_rel)
                    return b'{"version": "1.2.3"}'

                found = extract_version(
                    Path("."),
                    component_path,
                    {"file": "x.json", "field": "version"},
                    read_file_fn=reader,
                )
                self.assertEqual(seen, [expected])
                self.assertEqual(found, "1.2.3")

        refused = {
            "surrounding whitespace": "  svc  ",
            "parent segments": "../../etc",
            "parent segment in the middle": "svc/../..",
            "absolute": "/abs/path",
            "alternate stream": "svc:stream",
            "dot prefix": "./svc",
            "windows separator": "back\\slash",
        }
        for label, component_path in refused.items():
            with self.subTest(component_path=label):
                seen = []

                def reader(repo_rel: str) -> bytes:
                    seen.append(repo_rel)
                    return b'{"version": "1.2.3"}'

                found = extract_version(
                    Path("."),
                    component_path,
                    {"file": "x.json", "field": "version"},
                    read_file_fn=reader,
                )
                self.assertEqual(seen, [])
                self.assertIsNone(found)

    def test_the_shipped_accessor_refuses_the_escape_for_reasons_of_its_own(self):
        """Which is why the traversal is not reachable from the CLI today.

        The obligation calls the guarantee luck rather than design, and these
        are the messages that make that the right word: two source modes report
        a path missing from a captured tree and the third reports it untracked.
        Not one of them mentions containment.
        """
        self.assertEqual(set(ACCESSOR_REFUSALS), set(SOURCE_MODES))
        with _repository() as scene:
            scene.file("svc/package.json", '{"version": "1.0.0"}\n')
            scene.commit("add a version source")
            for mode in SOURCE_MODES:
                with self.subTest(source=mode):
                    with _SourceAccessor(scene.root, mode) as accessor:
                        self.assertEqual(
                            bytes(accessor.version_read_file("svc/package.json")),
                            b'{"version": "1.0.0"}\n',
                        )
                        with self.assertRaises(ValueError) as caught:
                            accessor.version_read_file("../outside/secret.json")
                    self.assertEqual(str(caught.exception), ACCESSOR_REFUSALS[mode])


class VersionSourceEncodingTests(unittest.TestCase):
    """OBL-CONFIG-041: one verdict per document, whatever the extension."""

    def _verdicts(self, encoding: str) -> Dict[str, Optional[str]]:
        transform = ENCODINGS[encoding]
        return {
            extension: _extract_field_from_bytes(
                transform(text), "manifest" + extension, "version"
            )
            for extension, text in VERSION_DOCUMENTS.items()
        }

    def _manifest_repository(self, extension: str, *, bom: bool = True) -> Scenario:
        """One component whose version source is written in *extension*."""
        name = "manifest" + extension
        scene = Scenario()
        scene.component(
            "svc",
            path="svc",
            provider="leaf",
            version_source={"file": name, "field": "version"},
        )
        (scene.root / "svc").mkdir(parents=True, exist_ok=True)
        body = VERSION_DOCUMENTS[extension].encode("utf-8") + b"\n"
        (scene.root / "svc" / name).write_bytes(
            (codecs.BOM_UTF8 if bom else b"") + body
        )
        scene.commit()
        return scene

    @requires_yaml
    def test_a_plain_utf8_manifest_reads_the_same_version_everywhere(self):
        """The premise: all four renderings do express the same document."""
        self.assertEqual(
            self._verdicts("utf-8"),
            {extension: "1.2.3" for extension in VERSION_DOCUMENTS},
        )

    @requires_yaml
    def test_a_utf16_document_is_refused_in_every_extension(self):
        self.assertEqual(
            self._verdicts("utf-16le with BOM"),
            {extension: None for extension in VERSION_DOCUMENTS},
        )

    @requires_yaml
    def test_a_lone_invalid_byte_is_refused_in_every_extension(self):
        self.assertEqual(
            self._verdicts("utf-8 with one invalid byte"),
            {extension: None for extension in VERSION_DOCUMENTS},
        )

    def test_the_shared_utf8_gate_strips_one_bom(self):
        raw = codecs.BOM_UTF8 + VERSION_DOCUMENTS[".json"].encode("utf-8")
        self.assertEqual(raw.decode("utf-8-sig"), VERSION_DOCUMENTS[".json"])

    @requires_yaml
    def test_a_byte_order_mark_produces_one_verdict_across_the_extensions(self):
        verdicts = self._verdicts("utf-8 with BOM")
        self.assertEqual(len(set(verdicts.values())), 1, verdicts)
        self.assertEqual(
            verdicts, {extension: "1.2.3" for extension in VERSION_DOCUMENTS}
        )

    @requires_yaml
    def test_the_bom_verdict_is_not_parser_dependent(self):
        self.assertEqual(
            self._verdicts("utf-8 with BOM"),
            {extension: "1.2.3" for extension in VERSION_DOCUMENTS},
        )

    def test_a_manifest_without_a_bom_generates_a_version_in_both_formats(self):
        """The premise: the two fixtures below differ only by three bytes."""
        extensions = [".json"] + ([".yaml"] if yaml is not None else [])
        for extension in extensions:
            with self.subTest(extension=extension):
                with self._manifest_repository(extension, bom=False) as scene:
                    result = run_cli(scene.root, "generate", "--source", "head")
                    self.assertEqual(result.returncode, EXIT_OK, result.stderr)
                    lock = json.loads(
                        (scene.root / "boundary.lock.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(lock["components"]["svc"]["version"], "1.2.3")

    def test_a_bom_prefixed_json_manifest_generates_the_version(self):
        with self._manifest_repository(".json") as scene:
            result = run_cli(scene.root, "generate", "--source", "head")
            self.assertEqual(result.returncode, EXIT_OK, result.stderr)
            lock = json.loads(
                (scene.root / "boundary.lock.json").read_text(encoding="utf-8")
            )
            self.assertEqual(lock["components"]["svc"]["version"], "1.2.3")

    @requires_yaml
    def test_a_bom_prefixed_yaml_manifest_generates_a_version_instead(self):
        """The other half of the split, reached through the real command."""
        with self._manifest_repository(".yaml") as scene:
            result = run_cli(scene.root, "generate", "--source", "head")
            self.assertEqual(result.returncode, EXIT_OK, result.stderr)
            lock = json.loads(
                (scene.root / "boundary.lock.json").read_text(encoding="utf-8")
            )
            self.assertEqual(lock["components"]["svc"]["version"], "1.2.3")

    def test_a_utf8_bom_is_not_misdiagnosed_as_a_missing_version(self):
        with self._manifest_repository(".json") as scene:
            result = run_cli(scene.root, "generate", "--source", "head")
            self.assertEqual(result.returncode, EXIT_OK, result.stderr)
            self.assertNotIn(GENERIC_NO_VERSION, result.stderr)


class DottedFieldPathTests(unittest.TestCase):
    """OBL-CONFIG-042: what the field grammar addresses, and what it cannot."""

    def _formats(self) -> List[str]:
        return [
            extension
            for extension in COLLIDING_DOCUMENTS
            if extension != ".yaml" or yaml is not None
        ]

    def _field_repository(self, field: str) -> Scenario:
        scene = Scenario()
        scene.component(
            "svc",
            path="svc",
            provider="leaf",
            version_source={"file": "manifest.json", "field": field},
        )
        (scene.root / "svc").mkdir(parents=True, exist_ok=True)
        (scene.root / "svc" / "manifest.json").write_bytes(
            COLLIDING_DOCUMENTS[".json"].encode("utf-8") + b"\n"
        )
        scene.commit()
        return scene

    def _validate_with_field(self, field: str) -> List[str]:
        with self._field_repository("version") as scene:
            config = json.loads(json.dumps(scene.config))
            config["components"]["svc"]["version_source"]["field"] = field
            return validate_config(config, scene.root)

    @requires_yaml
    def test_the_three_renderings_really_are_the_same_document(self):
        """The premise: parsers independent of boundver read all three alike."""
        oracles = {
            ".json": json.loads,
            ".toml": tomllib.loads,
            ".yaml": yaml.safe_load,
        }
        expected = {"version": "1.0.0", "a": {"b": "2.0.0"}, "a.b": UNREACHABLE_VALUE}
        for extension, text in COLLIDING_DOCUMENTS.items():
            with self.subTest(format=extension):
                self.assertEqual(oracles[extension](text), expected)
        for extension, text in LITERAL_ONLY_DOCUMENTS.items():
            with self.subTest(format=extension, document="literal only"):
                self.assertEqual(
                    oracles[extension](text), {"a.b": UNREACHABLE_VALUE}
                )

    def test_one_field_path_selects_one_node_in_every_format(self):
        for field, expected in FIELD_PATH_VERDICTS.items():
            for extension in self._formats():
                with self.subTest(field=field, format=extension):
                    self.assertEqual(
                        _extract_field_from_bytes(
                            COLLIDING_DOCUMENTS[extension].encode("utf-8"),
                            "manifest" + extension,
                            field,
                        ),
                        expected,
                    )

    def test_the_nesting_wins_and_the_literal_dotted_key_is_unreachable(self):
        for extension in self._formats():
            with self.subTest(format=extension):
                colliding = _extract_field_from_bytes(
                    COLLIDING_DOCUMENTS[extension].encode("utf-8"),
                    "manifest" + extension,
                    "a.b",
                )
                self.assertEqual(colliding, "2.0.0")
                self.assertNotEqual(colliding, UNREACHABLE_VALUE)
                alone = _extract_field_from_bytes(
                    LITERAL_ONLY_DOCUMENTS[extension].encode("utf-8"),
                    "manifest" + extension,
                    "a.b",
                )
                self.assertIsNone(alone)

    def test_configuration_validation_does_reject_some_field_values(self):
        """The premise: the branch that names version_source.field is live."""
        self.assertEqual(
            self._validate_with_field(" version"),
            [
                "Component 'svc' version_source.field must not have "
                "surrounding whitespace"
            ],
        )

    def test_a_degenerate_field_path_passes_configuration_validation(self):
        for field in DEGENERATE_FIELD_PATHS:
            with self.subTest(field=field):
                self.assertEqual(self._validate_with_field(field), [])

    def test_a_degenerate_field_path_is_refused_with_a_message_naming_it(self):
        """An unresolvable field path is named in the generate diagnostic.

        OBL-CONFIG-042 asks that "a..b", a trailing "version." and a bare "."
        either be rejected at validation with a message naming the field, or
        resolve. Validation accepts all three, and generate then fails with the
        diagnostic now identifies the declared path that failed to resolve.
        """
        with self._field_repository("a..b") as scene:
            result = run_cli(scene.root, "generate", "--source", "head")
            self.assertIn("a..b", result.stderr)

    def test_a_degenerate_field_path_fails_at_generate_time_with_context(self):
        for field in DEGENERATE_FIELD_PATHS:
            with self.subTest(field=field):
                with self._field_repository(field) as scene:
                    result = run_cli(scene.root, "generate", "--source", "head")
                    self.assertEqual(result.returncode, EXIT_USAGE, result.stderr)
                    self.assertIn(GENERIC_NO_VERSION, result.stderr)
                    self.assertIn(f"field '{field}'", result.stderr)
                    self.assertFalse((scene.root / "boundary.lock.json").exists())

    def test_a_resolvable_dotted_field_path_generates_the_nested_value(self):
        """The premise: this repository shape does produce a lock otherwise."""
        with self._field_repository("a.b") as scene:
            result = run_cli(scene.root, "generate", "--source", "head")
            self.assertEqual(result.returncode, EXIT_OK, result.stderr)
            lock = json.loads(
                (scene.root / "boundary.lock.json").read_text(encoding="utf-8")
            )
            self.assertEqual(lock["components"]["svc"]["version"], "2.0.0")


if __name__ == "__main__":
    unittest.main()
