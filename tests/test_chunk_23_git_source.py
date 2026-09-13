"""Six promises about the edges where boundver stops trusting its own input.

Five of these obligations are about the same fear from five directions. A
scheduler downstream reads a JSON payload and decides which tests to run; a
hashing path reads a Git record and decides what a component's digest is; a
deploy job reads an Action output and decides what to ship. In each case the
dangerous failure is not an error, it is a plausible answer that omits
something. So the tests here are written to detect quiet omission rather than
loud failure: a payload that violates its committed schema only on a failure
path, a parser that returns a half-populated record, a subprocess that hangs
instead of being killed, a value that closes its own heredoc and opens a new
output, a budget that resets between components so the second one looks
complete when the first said it was starved.

Three things made this awkward to write. The first is that most of these
states cannot be reached with a healthy repository, so eight broken ones are
generated here on purpose, alongside a clean control: a provider name the
registry does not know, a lock whose facet the config can no longer produce, a
boundary selector whose file was deleted after the lock was written, a
vendored copy edited out from under its source, a diagnostics list truncated
by a lowered ceiling, an explicitly empty slice, a repository with no commits
at all, and a `--depth 1` clone. Building each one correctly means writing the
lock *before* the breakage and committing the breakage after, because
`generate` fails closed on every one of them and a test that generated
afterwards would be testing an absent lockfile rather than a broken one.
The second is that the subprocess obligations cannot be observed with real
Git: no Git invocation stalls on demand, and the 300-second ceiling is far too
long to wait for. `_offline_git_command` is therefore redirected - only for
`ls-tree` and `cat-file`, so that the environment builder underneath keeps
using real Git - to a Python child that sleeps, floods stderr, or emits one
chunk and then sleeps, which is what makes "killed and reaped" an observation
rather than an inference. The third is that `_delimiter` picks its heredoc
delimiter from a SHA-256 of the value, so no value can be constructed that
contains its own delimiter; the module's `hashlib` is stubbed to a fixed
digest for exactly two tests, which is the only way to make the
`while candidate in occupied` loop actually run.

The parser accepts regular-file, executable-file, symlink, and gitlink entries
only in their canonical mode/type pairings. A submodule gitlink legitimately
uses object type `commit`; tree and every other object type fail closed because
the recursive listing consumed here should contain only files and gitlinks.

Covers OBL-GIT-SOURCE-073, OBL-GIT-SOURCE-074, OBL-GIT-SOURCE-076,
OBL-GIT-SOURCE-079, OBL-GIT-SOURCE-083 and OBL-GLOBS-005.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest import mock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from boundver import _git as git
from boundver import _provider_diff, _review, _utils, providers
from boundver._git import (
    MAX_GIT_DIAGNOSTIC_BYTES,
    MAX_GIT_PATH_BYTES,
    GitTreeEntry,
    _GitBlobSession,
    _git_cat_blob,
    _iter_git_blobs,
    _iter_git_nul_records,
    _parse_batch_header,
    _parse_ls_tree_record,
    _parse_name_status_entries,
    _repository_filter_config_overrides,
)
from boundver._utils import DIAGNOSTIC_TRUNCATION_SENTINEL, GuardrailError

from tests._parity import run_cli, run_cli_in_process
from tests._repo_fixtures import init_git_repo
from tests._scenarios import Scenario

try:  # jsonschema is a declared dev extra, not a runtime dependency.
    import jsonschema
except ImportError:  # pragma: no cover - exercised on hosts without the extra
    jsonschema = None

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = REPO_ROOT / "spec"

PROFILE = settings(
    max_examples=250,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much],
)

#: Streaming a fake process still builds a real Git environment underneath,
#: which costs one cached subprocess. Fewer examples, still every boundary.
STREAM_PROFILE = settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

#: One document that is valid JSON and a valid OpenAPI 3.1 description at
#: once, so a component can move between the raw and canonical providers
#: without its selector or its content changing.
def _contract(paths: int = 1) -> str:
    return (
        json.dumps(
            {
                "openapi": "3.1.0",
                "info": {"title": "t", "version": "1.0.0"},
                "paths": {
                    f"/p{index}": {
                        "get": {"responses": {"200": {"description": "ok"}}}
                    }
                    for index in range(1, paths + 1)
                },
            },
            indent=2,
        )
        + "\n"
    )


def _export_module():
    """The Action exporter, which lives in scripts/ rather than the package."""
    path = REPO_ROOT / "scripts" / "export_action_outputs.py"
    spec = importlib.util.spec_from_file_location("export_action_outputs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-073: every committed CLI schema, across every failure state
# ---------------------------------------------------------------------------

#: How each committed `spec/cli-output.<stem>.schema.json` is produced. The
#: value is a function of the built state, because review needs two immutable
#: endpoints that only the state knows. The keys are checked against the spec
#: directory at runtime, so a new committed schema fails this class until it
#: is driven here.
COMMAND_INVOCATIONS = {
    "verify": lambda state: ("verify", "--format", "json"),
    "status": lambda state: ("status", "--format", "json"),
    "diff": lambda state: (
        "diff",
        "boundary.lock.json",
        "boundary.lock.json",
        "--format",
        "json",
    ),
    "discover": lambda state: ("discover", "--format", "json"),
    "why": lambda state: ("why", "svc", "--format", "json"),
    "slice": lambda state: ("slice", "all", "--format", "json"),
    "review": lambda state: (
        "review",
        f"{state['base']}..{state['head']}",
        "--format",
        "json",
    ),
    "plan": lambda state: (
        "review",
        f"{state['base']}..{state['head']}",
        "--format",
        "plan",
    ),
    "migrate-lock": lambda state: (
        "migrate-lock",
        "--explain",
        "--format",
        "json",
    ),
    "coverage": lambda state: ("coverage", "--format", "json"),
}


def _base_scene(provider: str = "openapi", paths: int = 1) -> Scenario:
    """One component, one slice, one contract: the shape every state starts in."""
    scene = Scenario()
    scene.component("svc", path="svc", provider=provider, boundary=["api.json"])
    scene.slice("all", mode="exact", components=["svc"])
    scene.file("svc/api.json", _contract(paths))
    scene.file("svc/main.py", "print(1)\n")
    return scene


def _lock_and_commit(scene: Scenario, message: str = "lock") -> str:
    """Generate a lockfile, commit it, and return the resulting commit id."""
    result = run_cli(scene.root, "generate")
    if result.returncode != 0:  # pragma: no cover - a broken fixture, not a state
        raise AssertionError(f"fixture generate failed: {result.stderr}")
    scene.git("add", "--all")
    scene.git("commit", "-m", message)
    return scene.head()


def _state_clean() -> dict:
    scene = _base_scene()
    scene.commit()
    base = _lock_and_commit(scene)
    return {"scene": scene, "base": base, "head": base}


def _state_unknown_provider() -> dict:
    scene = _base_scene()
    scene.commit()
    base = _lock_and_commit(scene)
    scene.config["components"]["svc"]["boundary"]["provider"] = "no-such-provider"
    scene.write_config()
    scene.git("add", "--all")
    scene.git("commit", "-m", "unknown provider")
    return {"scene": scene, "base": base, "head": scene.head()}


def _state_unavailable_facet() -> dict:
    scene = _base_scene()
    scene.commit()
    base = _lock_and_commit(scene)
    scene.config["components"]["svc"]["boundary"] = {"provider": "leaf", "paths": []}
    scene.config["components"]["svc"]["verify_facets"] = ["boundary"]
    scene.write_config()
    scene.git("add", "--all")
    scene.git("commit", "-m", "leaf gates boundary")
    return {"scene": scene, "base": base, "head": scene.head()}


def _state_digest_error() -> dict:
    scene = _base_scene()
    scene.commit()
    base = _lock_and_commit(scene)
    scene.remove("svc/api.json")
    scene.git("add", "--all")
    scene.git("commit", "-m", "drop the boundary selector")
    return {"scene": scene, "base": base, "head": scene.head()}


def _state_vendored_divergence() -> dict:
    scene = Scenario()
    scene.component("svc", path="svc", provider="path-hash", boundary=["*.py"])
    scene.slice("all", mode="exact", components=["svc"])
    scene.config["components"]["svc"]["vendored_copies"] = ["vendor/svc"]
    scene.file("svc/main.py", "print(1)\n")
    scene.file("vendor/svc/main.py", "print(1)\n")
    scene.commit()
    base = _lock_and_commit(scene)
    scene.file("vendor/svc/main.py", "print(2)\n")
    scene.git("add", "--all")
    scene.git("commit", "-m", "diverge the vendored copy")
    return {"scene": scene, "base": base, "head": scene.head()}


def _state_empty_slice() -> dict:
    scene = _base_scene()
    scene.commit()
    base = _lock_and_commit(scene)
    scene.config["slices"]["nothing"] = {"mode": "exact", "components": []}
    scene.write_config()
    scene.git("add", "--all")
    scene.git("commit", "-m", "declare an empty slice")
    return {"scene": scene, "base": base, "head": scene.head()}


def _state_drift() -> dict:
    """Six drifting components, so a lowered diagnostic ceiling truncates."""
    scene = Scenario()
    for index in range(6):
        name = f"c{index}"
        scene.component(name, path=name, provider="path-hash", boundary=["*.py"])
        scene.file(f"{name}/main.py", "print(1)\n")
    scene.component("svc", path="svc", provider="path-hash", boundary=["*.py"])
    scene.slice("all", mode="exact", components=["svc"])
    scene.file("svc/main.py", "print(1)\n")
    scene.commit()
    base = _lock_and_commit(scene)
    for index in range(6):
        scene.file(f"c{index}/main.py", "print(2)\n")
    scene.git("add", "--all")
    scene.git("commit", "-m", "drift every component")
    return {"scene": scene, "base": base, "head": scene.head()}


def _state_unborn() -> dict:
    """A repository with a config on disk and no commit to capture."""
    directory = tempfile.TemporaryDirectory()
    root = Path(directory.name)
    init_git_repo(root)
    (root / "svc").mkdir()
    (root / "svc" / "api.json").write_text(_contract(), encoding="utf-8")
    (root / "boundary.config.json").write_text(
        json.dumps(
            {
                "project": "unborn",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": "openapi", "paths": ["api.json"]},
                    }
                },
                "slices": {"all": {"mode": "exact", "components": ["svc"]}},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {"directory": directory, "root": root, "base": "HEAD", "head": "HEAD"}


def _state_shallow() -> dict:
    """A --depth 1 clone: one commit reachable, the rest grafted away."""
    origin = _base_scene()
    origin.commit("one")
    _lock_and_commit(origin, "lock one")
    origin.file("svc/api.json", _contract(2))
    origin.commit("two")
    head = _lock_and_commit(origin, "lock two")
    directory = tempfile.TemporaryDirectory()
    clone = Path(directory.name) / "shallow"
    result = subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--no-local",
            origin.root.resolve().as_uri(),
            str(clone),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:  # pragma: no cover - a host without file:// clones
        raise AssertionError(f"fixture clone failed: {result.stderr}")
    return {
        "scene": origin,
        "directory": directory,
        "root": clone,
        "base": head,
        "head": head,
    }


#: Every generated repository/config/lock state this class drives, with the
#: string that proves the state actually manifested. Without the witness a
#: state that silently repaired itself would still "pass" every schema.
FAILURE_STATES = {
    "clean": (_state_clean, '"ok": true'),
    "unknown-provider": (
        _state_unknown_provider,
        "unsupported boundary.provider 'no-such-provider'",
    ),
    "unavailable-facet": (
        _state_unavailable_facet,
        "explicitly gates 'boundary' but provider 'leaf' has no boundary paths",
    ),
    "current-digest-error": (
        _state_digest_error,
        "CURRENT DIGEST ERROR svc: Declared boundary path matched no tracked files",
    ),
    "vendored-divergence": (
        _state_vendored_divergence,
        "Vendored copy at 'vendor/svc' differs from source",
    ),
    "empty-slice": (
        _state_empty_slice,
        "add a component name or remove the empty slice",
    ),
    "diagnostics-truncated": (_state_drift, DIAGNOSTIC_TRUNCATION_SENTINEL),
    "unborn": (_state_unborn, "HEAD does not resolve to a commit"),
    "shallow": (_state_shallow, '"repository_shallow": true'),
}


@unittest.skipIf(jsonschema is None, "jsonschema is not installed")
class MachineReadablePayloadsSurviveEveryFailureStateTests(unittest.TestCase):
    """OBL-GIT-SOURCE-073: the schemas hold on the paths nobody drives."""

    states: Dict[str, dict] = {}
    #: (state, command) -> the parsed payload that command printed in that
    #: state. Filled once by `_drive`, read by every test in the class.
    payloads: Dict[Tuple[str, str], object] = {}
    #: state -> everything the nine commands wrote, stdout and stderr both.
    transcripts: Dict[str, str] = {}
    #: (state, command) -> the schema violation the payload produced, if any.
    violations: Dict[Tuple[str, str], str] = {}
    driven = False

    @classmethod
    def setUpClass(cls):
        cls.states = {}
        cls.payloads = {}
        cls.transcripts = {}
        cls.violations = {}
        cls.driven = False
        for name, (build, _witness) in FAILURE_STATES.items():
            state = build()
            if "root" not in state:
                state["root"] = state["scene"].root
            cls.states[name] = state

    @classmethod
    def tearDownClass(cls):
        for state in cls.states.values():
            if "scene" in state:
                state["scene"].close()
            if "directory" in state:
                state["directory"].cleanup()
        cls.states = {}

    @staticmethod
    def _schema(stem: str) -> dict:
        return json.loads(
            (SPEC / f"cli-output.{stem}.schema.json").read_text(encoding="utf-8")
        )

    def test_the_command_table_names_every_committed_output_schema(self):
        """The enumeration, so a tenth schema cannot arrive undriven."""
        committed = {
            path.name.split(".")[1]
            for path in SPEC.glob("cli-output.*.schema.json")
        }
        self.assertEqual(committed, set(COMMAND_INVOCATIONS))

    @classmethod
    def _drive(cls):
        """Run every schema-bearing command in every failure state once.

        Recording rather than asserting: the tests below each read one
        projection of the same matrix, so a coverage hole and a schema
        violation are reported separately instead of one hiding the other.
        """
        if cls.driven:
            return
        for state_name, state in cls.states.items():
            transcript = []
            for command, build in COMMAND_INVOCATIONS.items():
                arguments = build(state)
                if state_name == "diagnostics-truncated":
                    with mock.patch.object(_utils, "MAX_DIAGNOSTIC_ITEMS", 4):
                        result = run_cli_in_process(state["root"], *arguments)
                else:
                    result = run_cli(state["root"], *arguments)
                transcript.append(result.stdout)
                transcript.append(result.stderr)
                if not result.stdout.strip():
                    continue
                payload = json.loads(result.stdout)
                cls.payloads[(state_name, command)] = payload
                try:
                    jsonschema.validate(payload, cls._schema(command))
                except jsonschema.ValidationError as error:
                    cls.violations[(state_name, command)] = str(error)
            cls.transcripts[state_name] = "".join(transcript)
        cls.driven = True

    def test_every_payload_produced_in_every_failure_state_validates(self):
        self._drive()
        for (state_name, command), payload in sorted(self.payloads.items()):
            with self.subTest(state=state_name, command=command):
                jsonschema.validate(payload, self._schema(command))
        self.assertEqual(self.violations, {})

    def test_every_generated_state_actually_reached_its_failure(self):
        """The premise: a state that quietly repaired itself is not evidence."""
        self._drive()
        for state_name, (_build, witness) in FAILURE_STATES.items():
            with self.subTest(state=state_name):
                self.assertIn(witness, self.transcripts[state_name])

    def test_every_committed_schema_was_exercised_by_some_state(self):
        """The premise: a schema no state reaches has not been validated."""
        self._drive()
        reached = {command for _state, command in self.payloads}
        self.assertEqual(reached, set(COMMAND_INVOCATIONS))

    def test_every_generated_state_produced_at_least_one_payload(self):
        self._drive()
        produced = {state for state, _command in self.payloads}
        self.assertEqual(produced, set(FAILURE_STATES))

    def test_a_deliberately_wrong_payload_is_rejected_by_the_same_check(self):
        """The premise: validate() would speak up. It has to be shown to.

        `additionalProperties: false` is what makes an unexpected failure-path
        key a violation, so the witness adds one rather than removing a
        required one.
        """
        self._drive()
        payload = dict(self.payloads[("clean", "verify")])
        payload["surprise"] = "a key no failure path is allowed to invent"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(payload, self._schema("verify"))

    def test_a_review_that_cannot_hold_every_row_emits_no_payload_at_all(self):
        """`complete` is true because a partial review is never printed.

        The byte budget is the one the obligation names. Lowering it must not
        produce a smaller review that still claims completeness; it must
        produce no review.
        """
        state = self.states["clean"]
        arguments = COMMAND_INVOCATIONS["review"](state)
        control = run_cli_in_process(state["root"], *arguments)
        self.assertEqual(control.returncode, 0)
        self.assertIs(json.loads(control.stdout)["complete"], True)
        with mock.patch.object(_review, "MAX_REVIEW_RESULT_BYTES", 64):
            starved = run_cli_in_process(state["root"], *arguments)
        self.assertEqual(starved.returncode, 2)
        self.assertEqual(starved.stdout.strip(), "")
        self.assertIn("No partial", starved.stderr)


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-074: the Git transport parsers on arbitrary bytes
# ---------------------------------------------------------------------------

#: A byte string that can appear as one NUL-delimited record: non-empty and
#: free of the terminator. Mapping rather than filtering keeps Hypothesis from
#: discarding most of what it draws.
RECORD = (
    st.binary(min_size=1, max_size=20)
    .map(lambda value: value.replace(b"\0", b"z"))
)

#: The alphabet an ls-tree record is built from when the point is to break it:
#: the two structural separators, the characters that make a field the wrong
#: shape, and one byte that is not ASCII at all.
LS_TREE_BYTE = st.sampled_from(
    [b" ", b"\t", b"0", b"1", b"6", b"a", b"f", b"g", b"A", b"/", b"\xff", b"\0"]
)

#: The one fake argv the streaming fixture answers; anything else is delegated
#: to real Git so that the environment builder underneath keeps working.
FAKE_GIT = "__boundver_fake_git__"


class _FakeGitProcess:
    """A Popen-shaped object serving fixed bytes, for chunk-boundary work."""

    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self._returncode = returncode
        self.killed = False

    def poll(self) -> int:
        return self._returncode

    def wait(self, timeout: Optional[float] = None) -> int:
        return self._returncode

    def kill(self) -> None:
        self.killed = True


class _FakeSubprocess:
    """`boundver._git.subprocess` with one argv redirected to fixed bytes."""

    def __init__(self, real, stdout: bytes):
        self._real = real
        self._stdout = stdout
        self.processes: List[_FakeGitProcess] = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def Popen(self, command, **kwargs):
        if command and command[0] == FAKE_GIT:
            process = _FakeGitProcess(self._stdout)
            self.processes.append(process)
            return process
        return self._real.Popen(command, **kwargs)


def _fake_listing_command(real):
    def build(repo_root, args):
        if args and args[0] == "ls-tree":
            return [FAKE_GIT]
        return real(repo_root, args)

    return build


def _reference_ls_tree(record: bytes) -> Optional[Tuple[str, str, str, bytes]]:
    """The `git ls-tree -z` grammar, written from the format not the code.

    Returns the four fields a well-formed record carries, or None when the
    record is not one.
    """
    header, separator, raw_path = record.partition(b"\t")
    if not separator or not raw_path:
        return None
    fields = header.split(b" ", 2)
    if len(fields) != 3:
        return None
    try:
        mode, object_type, oid = (field.decode("ascii") for field in fields)
    except UnicodeDecodeError:
        return None
    valid_mode_types = {
        "100644": "blob",
        "100755": "blob",
        "120000": "blob",
        "160000": "commit",
    }
    if valid_mode_types.get(mode) != object_type:
        return None
    if len(oid) not in {40, 64}:
        return None
    if any(character not in "0123456789abcdefABCDEF" for character in oid):
        return None
    return mode, object_type, oid, raw_path


#: A `cat-file --batch` stand-in whose header tells the truth.
HONEST_BATCH_SCRIPT = (
    "import sys\n"
    "sys.stdin.buffer.readline()\n"
    "sys.stdout.buffer.write(b'" + "a" * 40 + " blob 5\\nexact\\n')\n"
    "sys.stdout.buffer.flush()\n"
)

#: The same stand-in, promising a hundred bytes and delivering five.
SHORT_BATCH_SCRIPT = (
    "import sys\n"
    "sys.stdin.buffer.readline()\n"
    "sys.stdout.buffer.write(b'" + "a" * 40 + " blob 100\\nshort')\n"
    "sys.stdout.buffer.flush()\n"
)


class GitTransportParserFuzzTests(unittest.TestCase):
    """OBL-GIT-SOURCE-074: a typed error, or a record that re-encodes exactly."""

    def test_a_well_formed_record_parses_into_all_four_fields(self):
        """The premise: the parser does return records, so absence means something."""
        record = b"100644 blob " + b"a" * 40 + b"\tsvc/main.py"
        entry, path_bytes = _parse_ls_tree_record(record)
        self.assertEqual(
            entry,
            GitTreeEntry("svc/main.py", "100644", "blob", "a" * 40),
        )
        self.assertEqual(path_bytes, len(b"svc/main.py"))

    @PROFILE
    @given(record=st.lists(LS_TREE_BYTE, min_size=0, max_size=80).map(b"".join))
    def test_an_ls_tree_record_either_raises_or_re_encodes_to_its_input(self, record):
        """The oracle is the round trip, not a second copy of the rules.

        A partially populated entry cannot survive re-encoding: a truncated
        path, a dropped field or a silently normalised mode all change the
        bytes. So "no partial snapshot" becomes an equality rather than a
        hopeful absence.
        """
        try:
            entry, path_bytes = _parse_ls_tree_record(record)
        except (ValueError, GuardrailError, UnicodeDecodeError):
            return
        rebuilt = (
            f"{entry.mode} {entry.object_type} {entry.oid}\t".encode("ascii")
            + os.fsencode(entry.path)
        )
        self.assertEqual(rebuilt, record)
        self.assertNotEqual(entry.path, "")
        self.assertEqual(path_bytes, len(record) - record.index(b"\t") - 1)

    @PROFILE
    @given(record=st.lists(LS_TREE_BYTE, min_size=0, max_size=80).map(b"".join))
    def test_the_ls_tree_grammar_decides_acceptance(self, record):
        """Acceptance is exactly the documented grammar, over drawn bytes."""
        expected = _reference_ls_tree(record)
        try:
            entry, _path_bytes = _parse_ls_tree_record(record)
        except (ValueError, GuardrailError):
            self.assertIsNone(expected)
            return
        except UnicodeDecodeError:
            # The path decode is the filesystem codec's, not the parser's.
            self.assertIsNotNone(expected)
            return
        self.assertIsNotNone(expected)
        mode, object_type, oid, raw_path = expected
        self.assertEqual(
            (entry.mode, entry.object_type, entry.oid),
            (mode, object_type, oid),
        )
        self.assertEqual(os.fsencode(entry.path), raw_path)

    def test_an_over_long_path_is_a_guardrail_not_a_truncation(self):
        record = (
            b"100644 blob "
            + b"a" * 40
            + b"\t"
            + b"p" * (MAX_GIT_PATH_BYTES + 1)
        )
        with self.assertRaises(GuardrailError) as raised:
            _parse_ls_tree_record(record)
        self.assertEqual(
            str(raised.exception),
            f"Git path exceeds the {MAX_GIT_PATH_BYTES}-byte limit",
        )

    def test_the_object_type_allow_list_is_blob_and_commit(self):
        """A submodule is a commit; recursive listings contain no trees."""
        oid = b"a" * 40
        for mode, accepted in ((b"100644", b"blob"), (b"160000", b"commit")):
            with self.subTest(mode=mode.decode(), object_type=accepted.decode()):
                entry, _ = _parse_ls_tree_record(
                    mode + b" " + accepted + b" " + oid + b"\tsub"
                )
                self.assertEqual(entry.object_type, accepted.decode())
        for refused in (b"tree", b"tag", b"blobb", b""):
            with self.subTest(object_type=refused.decode()):
                with self.assertRaises(ValueError) as raised:
                    _parse_ls_tree_record(
                        b"040000 " + refused + b" " + oid + b"\tdir"
                    )
                self.assertIn("Git", str(raised.exception))

    def test_a_mode_outside_the_documented_set_is_refused(self):
        with self.assertRaises(ValueError):
            _parse_ls_tree_record(b"123456 blob " + b"a" * 40 + b"\tx")

    def test_only_canonical_mode_type_pairs_are_accepted(self):
        accepted = (
            (b"100644", b"blob"),
            (b"100755", b"blob"),
            (b"120000", b"blob"),
            (b"160000", b"commit"),
        )
        for mode, object_type in accepted:
            with self.subTest(mode=mode.decode(), object_type=object_type.decode()):
                entry, _ = _parse_ls_tree_record(
                    mode + b" " + object_type + b" " + b"a" * 40 + b"\tx"
                )
                self.assertEqual(entry.mode, mode.decode())

        for mode, object_type in (
            (b"040000", b"tree"),
            (b"100644", b"commit"),
            (b"160000", b"blob"),
            (b"123456", b"blob"),
            (b"000000", b"blob"),
            (b"999999", b"blob"),
            (b"10064", b"blob"),
            (b"1006440", b"blob"),
        ):
            with self.subTest(mode=mode.decode(), object_type=object_type.decode()):
                with self.assertRaises(ValueError) as raised:
                    _parse_ls_tree_record(
                        mode + b" " + object_type + b" " + b"a" * 40 + b"\tx"
                    )
                self.assertIn("Git", str(raised.exception))

    @PROFILE
    @given(
        header=st.lists(
            st.sampled_from([b" ", b"b", b"l", b"o", b"1", b"2", b"-", b"_",
                             b"+", b"\t", b"\xff", b"a"]),
            min_size=0,
            max_size=30,
        ).map(b"".join)
    )
    def test_a_batch_header_either_raises_or_returns_a_non_negative_size(self, header):
        try:
            size = _parse_batch_header(header, "ref")
        except ValueError:
            return
        self.assertIsInstance(size, int)
        self.assertGreaterEqual(size, 0)
        text = header.decode("ascii")
        last_space = text.rfind(" ")
        self.assertEqual(text[text.rfind(" ", 0, last_space) + 1:last_space], "blob")

    def test_a_batch_header_that_is_not_a_blob_never_yields_a_size(self):
        cases = {
            "missing object": (b"a" * 40 + b" missing", "Git blob not found"),
            "tree": (b"a" * 40 + b" tree 12", "Expected a Git blob"),
            "no separator": (b"abc", "Malformed git cat-file header"),
            "one separator": (b"blob 12", "Malformed git cat-file header"),
            "empty": (b"", "Malformed git cat-file header"),
            "non ascii": (b"\xff blob 12", "Malformed non-ASCII git cat-file"),
            "negative size": (b"a" * 40 + b" blob -5", "Negative git blob size"),
        }
        for label, (header, message) in cases.items():
            with self.subTest(header=label):
                with self.assertRaises(ValueError) as raised:
                    _parse_batch_header(header, "ref")
                self.assertIn(message, str(raised.exception))

    def test_the_size_field_currently_accepts_more_than_git_can_emit(self):
        """Pinned laxity: `int()` is more generous than the batch protocol.

        Underscore grouping, a leading plus and leading whitespace all parse.
        Git emits none of them, and a wrong size fails the following
        fixed-length read rather than yielding content, so this is recorded
        rather than treated as a hole - but it is recorded.
        """
        for text, expected in ((b" blob 1_0", 10), (b" blob +5", 5), (b" blob \t5", 5)):
            with self.subTest(size=text.decode()):
                self.assertEqual(
                    _parse_batch_header(b"a" * 40 + text, "ref"), expected
                )

    def _scripted_read(self, script: str) -> bytes:
        """Answer one `cat-file --batch` request with hand-written bytes."""
        real = git._offline_git_command
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/main.py", "print(1)\n")
            scene.commit()
            with mock.patch.object(
                git, "_offline_git_command", _script_command(real, script)
            ):
                session = _GitBlobSession(scene.root)
                try:
                    return session.read_blob("a" * 40)
                finally:
                    try:
                        session.close()
                    except Exception:
                        pass

    def test_a_batch_reply_matching_its_header_is_returned_whole(self):
        """The premise for the size-disagreement test below."""
        self.assertEqual(self._scripted_read(HONEST_BATCH_SCRIPT), b"exact")

    def test_a_declared_size_that_disagrees_with_the_delivered_bytes_raises(self):
        """A header promising 100 bytes and delivering 5 yields no content.

        This is the shape `spec/HASHING.md` calls fail-closed: the read must
        not return the five bytes it got, because a short blob hashes to a
        stable, wrong digest.
        """
        with self.assertRaises(ValueError) as raised:
            self._scripted_read(SHORT_BATCH_SCRIPT)
        self.assertIn("Truncated git cat-file content", str(raised.exception))

    @PROFILE
    @given(
        fields=st.lists(
            st.sampled_from(
                [b"M", b"A", b"D", b"R100", b"C100", b"Z", b"", b"\xff",
                 b"a.txt", b"b.txt", b"p" * 4]
            ),
            min_size=0,
            max_size=8,
        )
    )
    def test_name_status_entries_either_raise_or_pair_every_field(self, fields):
        try:
            entries = _parse_name_status_entries(fields)
        except (ValueError, GuardrailError):
            return
        for status, path in entries:
            self.assertTrue(status)
            self.assertNotEqual(path, "")
            self.assertIn(status[0], "ACDMRTUXB")
        # Every accepted field list is consumed whole. A rename group spends
        # three fields for two rows, a copy group three fields for one, and
        # every other group two fields for one, so the field count is a
        # function of the rows and nothing is left over or invented.
        renames = sum(1 for status, _ in entries if status.startswith("R"))
        copies = sum(1 for status, _ in entries if status.startswith("C"))
        plain = len(entries) - renames - copies
        self.assertEqual(len(fields), 3 * (renames // 2 + copies) + 2 * plain)

    def test_a_truncated_name_status_stream_never_returns_a_short_list(self):
        cases = {
            "status with no path": ([b"M"], "Truncated path in Git diff output"),
            "rename with one path": (
                [b"R100", b"a.txt"],
                "Truncated rename/copy in Git diff output",
            ),
            "unknown status": ([b"Z", b"a.txt"], "Malformed Git diff status"),
            "empty status": ([b"", b"a.txt"], "Malformed Git diff status"),
            "empty path": ([b"M", b""], "Malformed empty path in Git output"),
        }
        for label, (fields, message) in cases.items():
            with self.subTest(stream=label):
                with self.assertRaises(ValueError) as raised:
                    _parse_name_status_entries(fields)
                self.assertIn(message, str(raised.exception))
        with self.assertRaises(GuardrailError):
            _parse_name_status_entries([b"M", b"p" * (MAX_GIT_PATH_BYTES + 1)])


class NulRecordChunkBoundaryTests(unittest.TestCase):
    """OBL-GIT-SOURCE-074: the streaming framing, split everywhere."""

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario()
        cls.scene.component("svc", path="svc", provider="leaf")
        cls.scene.file("svc/main.py", "print(1)\n")
        cls.scene.commit()
        # Warm the filter-config cache once, so the fake-transport examples do
        # not each pay for a real Git subprocess while building the env.
        git._offline_git_environment(cls.scene.root)

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()
        _repository_filter_config_overrides.cache_clear()

    def setUp(self):
        self.real_command = git._offline_git_command

    def _drain(self, stream: bytes, chunk: int) -> Tuple[List[bytes], object]:
        """Run the real generator over *stream*, delivered *chunk* bytes at a time."""
        shim = _FakeSubprocess(subprocess, stream)
        yielded: List[bytes] = []
        failure: object = None
        with mock.patch.object(git, "subprocess", shim), mock.patch.object(
            git, "_GIT_STREAM_CHUNK_BYTES", chunk
        ), mock.patch.object(
            git, "_offline_git_command", _fake_listing_command(self.real_command)
        ):
            try:
                for record in _iter_git_nul_records(
                    self.scene.root, ["ls-tree", "-z", "HEAD"]
                ):
                    yielded.append(record)
            except BaseException as exc:  # noqa: BLE001 - the assertion is the type
                failure = exc
        return yielded, failure

    def test_the_fixture_delivers_records_through_the_real_generator(self):
        """The premise: without it, every absence below would be vacuous."""
        yielded, failure = self._drain(b"one\0two\0", 64 * 1024)
        self.assertIsNone(failure)
        self.assertEqual(yielded, [b"one", b"two"])

    @STREAM_PROFILE
    @given(
        records=st.lists(RECORD, min_size=1, max_size=8),
        chunk=st.integers(min_value=1, max_value=48),
    )
    def test_a_terminated_stream_yields_its_records_at_every_chunk_size(
        self, records, chunk
    ):
        """The oracle is the list that was written, not a second parser."""
        stream = b"".join(record + b"\0" for record in records)
        yielded, failure = self._drain(stream, chunk)
        self.assertIsNone(failure)
        self.assertEqual(yielded, records)

    @STREAM_PROFILE
    @given(
        records=st.lists(RECORD, min_size=1, max_size=8),
        chunk=st.integers(min_value=1, max_value=48),
    )
    def test_a_final_record_without_its_terminator_raises_after_the_prefix(
        self, records, chunk
    ):
        """Pending bytes are never a record: the stream fails closed instead."""
        stream = b"".join(record + b"\0" for record in records)[:-1]
        yielded, failure = self._drain(stream, chunk)
        self.assertIsInstance(failure, ValueError)
        self.assertEqual(
            str(failure), "Truncated NUL-delimited Git listing output"
        )
        self.assertEqual(yielded, records[:-1])

    def test_an_empty_record_is_skipped_rather_than_yielded_as_empty_bytes(self):
        yielded, failure = self._drain(b"one\0\0\0two\0", 3)
        self.assertIsNone(failure)
        self.assertEqual(yielded, [b"one", b"two"])


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-076: the three baseline flags, and what they refuse
# ---------------------------------------------------------------------------

#: The three flags the argparse group holds, each with a path argument.
BASELINE_FLAGS = ("--baseline", "--write-baseline", "--update-baseline")

#: The two guards in `core._cmd_verify`, keyed by the flag that trips them.
GUARD_MESSAGES = {
    "--update": (
        "ERROR: verification baselines cannot be combined with lockfile --update"
    ),
    "--fail-fast": (
        "ERROR: verification baselines require the complete issue set; "
        "remove --fail-fast"
    ),
}


class BaselineOptionExclusionTests(unittest.TestCase):
    """OBL-GIT-SOURCE-076: refused before anything is read or written."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        init_git_repo(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def _paths(self) -> List[str]:
        return sorted(
            path.name for path in self.root.iterdir() if path.name != ".git"
        )

    def test_an_unborn_repository_reaches_the_snapshot_without_the_guards(self):
        """The premise: this is how far verify gets when nothing is refused.

        Every assertion below is that a specific message appears *instead of*
        this one. Without this test the guard assertions would not distinguish
        "refused early" from "refused for some other reason".
        """
        for flag in (*BASELINE_FLAGS, "--update", "--fail-fast"):
            with self.subTest(flag=flag):
                arguments = (
                    (flag, "b.json") if flag in BASELINE_FLAGS else (flag,)
                )
                result = run_cli(self.root, "verify", *arguments)
                self.assertEqual(result.returncode, 2)
                self.assertIn(
                    "Cannot capture head source: HEAD does not resolve to a commit",
                    result.stderr,
                )
                self.assertEqual(self._paths(), [])

    def test_two_baseline_flags_together_are_refused_by_the_argparse_group(self):
        pairs = [
            ("--baseline", "--write-baseline"),
            ("--baseline", "--update-baseline"),
            ("--write-baseline", "--update-baseline"),
        ]
        for first, second in pairs:
            with self.subTest(first=first, second=second):
                result = run_cli(
                    self.root, "verify", first, "one.json", second, "two.json"
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(
                    f"argument {second}: not allowed with argument {first}",
                    result.stderr,
                )
                self.assertEqual(result.stdout, "")
                self.assertEqual(self._paths(), [])

    def test_a_baseline_flag_with_update_or_fail_fast_is_refused_by_name(self):
        for flag in BASELINE_FLAGS:
            for conflicting, message in GUARD_MESSAGES.items():
                with self.subTest(baseline=flag, conflicting=conflicting):
                    result = run_cli(
                        self.root, "verify", flag, "baseline.json", conflicting
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stderr.strip(), message)
                    self.assertEqual(result.stdout, "")

    def test_the_refusal_happens_before_the_snapshot_the_config_or_a_file(self):
        """Ordering, observed rather than assumed.

        This repository has no commit and no config, so reaching either would
        say so on stderr - as the premise test above shows it does. Seeing the
        guard's message instead is the evidence that neither was reached, and
        the empty directory listing is the evidence that no baseline was
        written on the way past.
        """
        for flag in BASELINE_FLAGS:
            for conflicting, message in GUARD_MESSAGES.items():
                with self.subTest(baseline=flag, conflicting=conflicting):
                    result = run_cli(
                        self.root, "verify", flag, "baseline.json", conflicting
                    )
                    self.assertEqual(result.stderr.strip(), message)
                    self.assertNotIn("Cannot capture head source", result.stderr)
                    self.assertNotIn("boundary.config.json", result.stderr)
                    self.assertEqual(self._paths(), [])


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-079: the wall clock, the stderr drain, and the abandoned child
# ---------------------------------------------------------------------------

#: A Python child standing in for a Git that never finishes.
STALL_SCRIPT = "import time\ntime.sleep(60)\n"

#: One chunk of records and then silence: enough for the generator to yield,
#: not enough for it to finish, which is what "abandoned mid-stream" needs.
CHUNK_THEN_STALL_SCRIPT = (
    "import sys, time\n"
    "sys.stdout.buffer.write(b'alpha\\x00beta\\x00' * 4)\n"
    "sys.stdout.buffer.flush()\n"
    "time.sleep(60)\n"
)

#: Far more stderr than the diagnostic ceiling, written before exiting cleanly.
FLOOD_SCRIPT = (
    "import sys\n"
    "sys.stderr.buffer.write(b'E' * 400000)\n"
    "sys.stderr.buffer.flush()\n"
)

#: Records and a clean exit, for the premise that the redirection works.
FAST_SCRIPT = "import sys\nsys.stdout.buffer.write(b'one\\x00two\\x00')\n"


class _RecordingSubprocess:
    """`boundver._git.subprocess`, keeping every child it started.

    Real Git children are started too - the environment builder underneath
    runs `git config` - so `scripted` separates the redirected stand-ins,
    which are the only ones an assertion about reaping may look at.
    """

    def __init__(self, real):
        self._real = real
        self.processes: List[subprocess.Popen] = []
        self.scripted: List[subprocess.Popen] = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def Popen(self, command, **kwargs):
        process = self._real.Popen(command, **kwargs)
        self.processes.append(process)
        if command and command[0] == sys.executable:
            self.scripted.append(process)
        return process


def _script_command(real, script: str):
    """Redirect only the two streaming subcommands to a Python child."""

    def build(repo_root, args):
        if args and args[0] in {"ls-tree", "cat-file"}:
            return [sys.executable, "-c", script]
        return real(repo_root, args)

    return build


class GitStreamingDeadlineAndDrainTests(unittest.TestCase):
    """OBL-GIT-SOURCE-079: every streaming Popen, killed and reaped."""

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario()
        cls.scene.component("svc", path="svc", provider="leaf")
        cls.scene.file("svc/main.py", "print(1)\n")
        cls.scene.commit()
        cls.oid = cls.scene.git("rev-parse", "HEAD:svc/main.py")

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()
        _repository_filter_config_overrides.cache_clear()

    def setUp(self):
        self.real_command = git._offline_git_command
        _repository_filter_config_overrides.cache_clear()

    def tearDown(self):
        _repository_filter_config_overrides.cache_clear()

    # ---- the surface, read from the source rather than listed -------------

    @staticmethod
    def _popen_owners() -> Dict[str, int]:
        """Every function in `_git.py` that starts a Git child, by name."""
        source = (REPO_ROOT / "src" / "boundver" / "_git.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        functions = [
            (node.lineno, node.end_lineno, node.name)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        owners: Dict[str, int] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if not (
                isinstance(target, ast.Attribute)
                and target.attr == "Popen"
                and isinstance(target.value, ast.Name)
                and target.value.id == "subprocess"
            ):
                continue
            enclosing = [
                name
                for start, end, name in functions
                if start <= node.lineno <= end
            ]
            owners[enclosing[-1]] = owners.get(enclosing[-1], 0) + 1
        return owners

    def _drivers(self):
        """One driver per streaming Popen site, keyed by its function name."""
        root = self.scene.root
        return {
            "_iter_git_nul_records": lambda: list(
                _iter_git_nul_records(root, ["ls-tree", "-z", "HEAD"])
            ),
            "_git_cat_blob": lambda: _git_cat_blob(root, self.oid),
            "_start": lambda: _GitBlobSession(root).read_blob(self.oid),
            "_iter_git_blobs": lambda: list(_iter_git_blobs(root, [self.oid])),
        }

    def test_every_streaming_popen_site_in_git_py_has_a_driver_here(self):
        """The enumeration: a new streaming path fails this, not CI at 3am."""
        owners = self._popen_owners()
        self.assertEqual(
            set(owners),
            {"_git_run", *self._drivers()},
            "an unrecognised subprocess.Popen site appeared in _git.py",
        )
        # `_git_run` is the buffered path, covered elsewhere by the existing
        # suite; every other site is a streaming one and is driven below.
        self.assertEqual(owners["_git_run"], 1)

    # ---- the wall clock ---------------------------------------------------

    def test_the_redirected_child_still_reaches_the_real_streaming_code(self):
        """The premise for every deadline assertion that follows."""
        with mock.patch.object(
            git,
            "_offline_git_command",
            _script_command(self.real_command, FAST_SCRIPT),
        ):
            records = list(
                _iter_git_nul_records(self.scene.root, ["ls-tree", "-z", "HEAD"])
            )
        self.assertEqual(records, [b"one", b"two"])

    def test_a_stalled_child_is_killed_and_reported_on_every_streaming_path(self):
        for name, drive in self._drivers().items():
            with self.subTest(path=name):
                _repository_filter_config_overrides.cache_clear()
                recorder = _RecordingSubprocess(subprocess)
                with mock.patch.object(git, "subprocess", recorder), \
                        mock.patch.object(git, "MAX_GIT_COMMAND_SECONDS", 0.5), \
                        mock.patch.object(
                            git,
                            "_offline_git_command",
                            _script_command(self.real_command, STALL_SCRIPT),
                        ):
                    with self.assertRaises(GuardrailError) as raised:
                        drive()
                self.assertEqual(
                    str(raised.exception),
                    "Git command exceeds the 0.5-second wall-clock limit",
                )
                self.assertTrue(recorder.scripted, "no stand-in child was started")
                for process in recorder.scripted:
                    self.assertIsNotNone(
                        process.poll(), "a stalled child was left running"
                    )

    # ---- the abandoned generator ------------------------------------------

    def test_abandoning_the_record_generator_mid_stream_reaps_its_child(self):
        recorder = _RecordingSubprocess(subprocess)
        with mock.patch.object(git, "subprocess", recorder), \
                mock.patch.object(git, "_GIT_STREAM_CHUNK_BYTES", 12), \
                mock.patch.object(
                    git,
                    "_offline_git_command",
                    _script_command(self.real_command, CHUNK_THEN_STALL_SCRIPT),
                ):
            records = _iter_git_nul_records(
                self.scene.root, ["ls-tree", "-z", "HEAD"]
            )
            self.assertEqual(next(records), b"alpha")
            live = list(recorder.scripted)
            self.assertTrue(live, "no stand-in child was started")
            # The premise for the assertion below: the child really is still
            # running at the moment the generator is dropped. Without this the
            # "reaped" assertion would hold over a process that had already
            # exited on its own.
            for process in live:
                self.assertIsNone(process.poll(), "the child exited before abandonment")
            records.close()
        for process in live:
            self.assertIsNotNone(process.poll(), "an abandoned child was leaked")

    # ---- the bounded stderr drain -----------------------------------------

    def test_a_stderr_flood_is_bounded_and_reported_rather_than_spooled(self):
        recorder = _RecordingSubprocess(subprocess)
        with mock.patch.object(git, "subprocess", recorder), \
                mock.patch.object(
                    git,
                    "_offline_git_command",
                    _script_command(self.real_command, FLOOD_SCRIPT),
                ):
            with self.assertRaises(GuardrailError) as raised:
                list(_iter_git_nul_records(self.scene.root, ["ls-tree", "-z", "HEAD"]))
        self.assertEqual(
            str(raised.exception),
            f"Git command stderr exceeds the {MAX_GIT_DIAGNOSTIC_BYTES}-byte limit",
        )
        self.assertTrue(recorder.scripted, "no stand-in child was started")
        for process in recorder.scripted:
            self.assertIsNotNone(process.poll())

    def test_the_drain_retains_no_more_than_the_diagnostic_ceiling(self):
        """The bound itself, read off the drain rather than inferred."""
        recorder = _RecordingSubprocess(subprocess)
        captured = {}
        real_drain = git._BoundedGitDiagnosticDrain

        class _Observed(real_drain):
            def __init__(self, process):
                super().__init__(process)
                captured["drain"] = self

        with mock.patch.object(git, "subprocess", recorder), \
                mock.patch.object(git, "_BoundedGitDiagnosticDrain", _Observed), \
                mock.patch.object(
                    git,
                    "_offline_git_command",
                    _script_command(self.real_command, FLOOD_SCRIPT),
                ):
            with self.assertRaises(GuardrailError):
                list(_iter_git_nul_records(self.scene.root, ["ls-tree", "-z", "HEAD"]))
        drain = captured["drain"]
        self.assertLessEqual(len(drain.snapshot()), MAX_GIT_DIAGNOSTIC_BYTES)
        self.assertIsInstance(drain._data, bytearray)


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-083: $GITHUB_OUTPUT, parsed the way the runner parses it
# ---------------------------------------------------------------------------

#: The characters `str.splitlines` treats as line breaks and the GitHub runner
#: does not, plus the two the runner does. A value carrying any of them is
#: where `occupied` and the runner's view could disagree.
EXOTIC_BREAKS = ("\x0b", "\x0c", "\x85", "\u2028", "\u2029", "\r", "\n")

#: What a hostile component name or source path is built from here. The
#: fragments are multi-character on purpose - `<<`, `::` and the delimiter
#: prefix are the sequences that mean something to a parser - so the value is
#: assembled from a list rather than drawn as text over an alphabet.
HOSTILE_TEXT = st.lists(
    st.sampled_from(
        [
            *EXOTIC_BREAKS,
            "=",
            "<",
            "<<",
            "%",
            ":",
            ",",
            "::",
            "BOUNDVER_OUTPUT_",
            "a",
            " ",
        ]
    ),
    max_size=12,
).map("".join)

#: The digest `_delimiter` is stubbed to return, so a value can be written
#: that contains the delimiter the function is about to choose.
FIXED_DIGEST = "0" * 64


class _FixedHashlib:
    """`hashlib` with a sha256 whose hexdigest never varies."""

    class _Digest:
        @staticmethod
        def hexdigest() -> str:
            return FIXED_DIGEST

    @staticmethod
    def sha256(_data: bytes) -> "_FixedHashlib._Digest":
        return _FixedHashlib._Digest()


def parse_github_output(text: str) -> List[Tuple[str, str]]:
    """Parse a $GITHUB_OUTPUT file the way the Actions runner does.

    The runner splits the file into lines on CR, LF or CRLF, then for each
    line compares the position of the first `=` with the position of the first
    `<<`: whichever comes first decides whether the line is a direct
    assignment or opens a heredoc, and a heredoc runs until a line that equals
    its delimiter exactly. This is written from that rule, not from
    `_append_output`, so it is an oracle rather than a mirror.
    """
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalised.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    pairs: List[Tuple[str, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        equals = line.find("=")
        heredoc = line.find("<<")
        if equals >= 0 and (heredoc < 0 or equals < heredoc):
            pairs.append((line[:equals], line[equals + 1:]))
            continue
        if heredoc >= 0 and (equals < 0 or equals > heredoc):
            name, delimiter = line[:heredoc], line[heredoc + 2:]
            body: List[str] = []
            while True:
                if index >= len(lines):
                    raise ValueError(f"unterminated heredoc for {name!r}")
                if lines[index] == delimiter:
                    index += 1
                    break
                body.append(lines[index])
                index += 1
            pairs.append((name, "\n".join(body)))
            continue
        raise ValueError(f"unparseable $GITHUB_OUTPUT line {line!r}")
    return pairs


def parse_workflow_command(line: str) -> Tuple[str, Dict[str, str], str]:
    """Parse one `::name prop=value,prop=value::message` annotation.

    Percent-decoding is applied to the properties and the message, which is
    what makes "a path cannot add a second property" an equality rather than a
    hopeful absence.
    """
    if not line.startswith("::"):
        raise ValueError(f"not a workflow command: {line!r}")
    body = line[2:]
    head, separator, message = body.partition("::")
    if not separator:
        raise ValueError(f"workflow command has no message: {line!r}")
    name, _, property_text = head.partition(" ")

    def decode(value: str) -> str:
        return (
            value.replace("%3A", ":")
            .replace("%2C", ",")
            .replace("%0D", "\r")
            .replace("%0A", "\n")
            .replace("%25", "%")
        )

    properties: Dict[str, str] = {}
    if property_text:
        for item in property_text.split(","):
            key, _, value = item.partition("=")
            properties[key] = decode(value)
    return name, properties, decode(message)


class ActionOutputInjectionTests(unittest.TestCase):
    """OBL-GIT-SOURCE-083: no value may end its heredoc or open an output."""

    def setUp(self):
        self.module = _export_module()
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def _write(self, pairs) -> str:
        target = self.root / "github_output"
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            for name, value in pairs:
                self.module._append_output(handle, name, value)
        return target.read_text(encoding="utf-8")

    @staticmethod
    def _expected(value: str) -> str:
        """What the runner recovers for a value the writer terminated itself.

        The writer appends one LF before the closing delimiter, so a value
        that already ends in CR forms a CRLF with it and the runner counts one
        line break where two characters were written. Modelling the writer's
        own newline is what makes this exact rather than approximate, and it
        collapses to the identity for every value with no CR in it.
        """
        terminated = (value + "\n").replace("\r\n", "\n").replace("\r", "\n")
        return terminated[:-1]

    def test_an_ordinary_pair_round_trips_through_the_runner_parser(self):
        """The premise: the oracle reads what the writer writes."""
        text = self._write([("issues", "one issue"), ("transport-complete", "true")])
        self.assertEqual(
            parse_github_output(text),
            [("issues", "one issue"), ("transport-complete", "true")],
        )

    def test_the_runner_parser_notices_an_unguarded_injection(self):
        """The premise for every "no extra output" assertion below.

        Written by hand rather than through `_append_output`, because the
        point is that the oracle can see an injection when one is present.
        """
        forged = "issues<<D\nsomething\nD\ninjected=deploy-everything\n"
        self.assertEqual(
            parse_github_output(forged),
            [("issues", "something"), ("injected", "deploy-everything")],
        )

    @PROFILE
    @given(
        values=st.lists(HOSTILE_TEXT, min_size=1, max_size=4),
    )
    def test_no_hostile_value_adds_or_loses_an_output(self, values):
        target = self.root / "github_output"
        if target.exists():
            target.unlink()
        pairs = [(f"out{index}", value) for index, value in enumerate(values)]
        recovered = parse_github_output(self._write(pairs))
        self.assertEqual(
            [name for name, _ in recovered], [name for name, _ in pairs]
        )
        for (_name, written), (_recovered_name, read_back) in zip(pairs, recovered):
            self.assertEqual(read_back, self._expected(written))

    def test_a_value_containing_the_chosen_delimiter_cannot_terminate_itself(self):
        """The `while candidate in occupied` loop, actually run.

        No value can be constructed that contains its own SHA-256, so the
        module's `hashlib` is stubbed for the length of this test. The premise
        test below shows the stub alone does not lengthen the delimiter.
        """
        candidate = f"BOUNDVER_OUTPUT_{FIXED_DIGEST}"
        value = "\n".join(["before", candidate, candidate + "X", "after"])
        with mock.patch.object(self.module, "hashlib", _FixedHashlib):
            delimiter = self.module._delimiter("issues", value)
            text = self._write([("issues", value), ("next", "kept")])
        self.assertEqual(delimiter, candidate + "XX")
        self.assertEqual(
            parse_github_output(text), [("issues", value), ("next", "kept")]
        )

    def test_the_stub_alone_leaves_the_delimiter_unlengthened(self):
        """The premise: the X suffix above came from the loop, not the stub."""
        with mock.patch.object(self.module, "hashlib", _FixedHashlib):
            self.assertEqual(
                self.module._delimiter("issues", "nothing to collide with"),
                f"BOUNDVER_OUTPUT_{FIXED_DIGEST}",
            )

    def test_a_value_broken_only_by_an_exotic_separator_stays_one_output(self):
        """Where `splitlines` and the runner disagree, `occupied` is the superset."""
        candidate = f"BOUNDVER_OUTPUT_{FIXED_DIGEST}"
        for separator in ("\x0b", "\x0c", "\x85", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                target = self.root / "github_output"
                if target.exists():
                    target.unlink()
                value = f"a{separator}{candidate}{separator}b"
                with mock.patch.object(self.module, "hashlib", _FixedHashlib):
                    text = self._write([("issues", value)])
                # No CR or LF, so this is a direct assignment and the exotic
                # break travels inside the value where the runner cannot see it.
                self.assertEqual(parse_github_output(text), [("issues", value)])

    def test_the_exporter_escapes_every_value_it_routes_through_the_bounder(self):
        """The coupling the register warned about, pinned at the exporter.

        `result-file` reaches `_append_output` without passing through
        `_bounded_transport_text`, which is exactly why the heredoc has to be
        safe on its own. Everything repository-controlled must arrive already
        escaped, and this asserts that on a payload built to break it.
        """
        hostile = (
            "svc\x0b" + f"BOUNDVER_OUTPUT_{FIXED_DIGEST}" + "\ninjected=yes\r::notice"
        )
        result = self.root / "result.json"
        result.write_text(
            json.dumps(
                {
                    "ok": False,
                    "issues": [hostile],
                    "observations": [hostile],
                    "consumer_impact": [],
                }
            ),
            encoding="utf-8",
        )
        output = self.root / "github_output"
        output.write_text("", encoding="utf-8")
        self.module.export_outputs(result, output, operation="verify")
        recovered = dict(parse_github_output(output.read_text(encoding="utf-8")))
        expected = {
            *self.module.SIZED_OUTPUTS,
            *self.module.PLAN_ARRAY_OUTPUTS,
            "truncated-outputs",
            "result-schema",
            "transport-complete",
            "selection-complete",
            "result-file",
            "summary-file",
        }
        self.assertEqual(set(recovered), expected)
        for name in ("issues", "observations"):
            with self.subTest(output=name):
                self.assertNotIn("\n", recovered[name])
                self.assertNotIn("\r", recovered[name])
                self.assertIn("\\x0b", recovered[name])
                self.assertIn("\\n", recovered[name])
                self.assertIn("\\r", recovered[name])

    @PROFILE
    @given(text=HOSTILE_TEXT)
    def test_a_workflow_property_can_never_open_a_second_field(self, text):
        rendered = self.module._workflow_property(text)
        for forbidden in (":", ",", "\r", "\n"):
            self.assertNotIn(forbidden, rendered)
        line = (
            "::notice file="
            + rendered
            + ",title=Boundver structural change::"
            + self.module._workflow_message("a message")
        )
        name, properties, message = parse_workflow_command(line)
        self.assertEqual(name, "notice")
        self.assertEqual(sorted(properties), ["file", "title"])
        self.assertEqual(properties["title"], "Boundver structural change")
        self.assertEqual(message, "a message")

    def test_an_unescaped_path_would_have_opened_a_second_field(self):
        """The premise: the annotation oracle can see the injection."""
        raw = "svc,title=Forged,line=1"
        name, properties, _message = parse_workflow_command(
            f"::notice file={raw},title=Real::message"
        )
        self.assertEqual(name, "notice")
        self.assertEqual(sorted(properties), ["file", "line", "title"])
        self.assertEqual(properties["title"], "Real")

    def test_a_crafted_source_path_reaches_the_annotation_percent_encoded(self):
        hostile = "svc/a,title=Forged,line=1:x%y\nz"
        commit = "c" * 40
        payload = {
            "endpoints": {"target": {"commit": commit}},
            "source_locations": [
                {
                    "component": "svc",
                    "path": hostile,
                    "endpoint": "target",
                    "commit": commit,
                    "kind": "structural-document",
                }
            ],
        }
        stream = io.StringIO()
        with mock.patch.object(self.module.sys, "stderr", stream):
            self.module._emit_source_annotations(payload, commit)
        lines = [line for line in stream.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 1)
        name, properties, message = parse_workflow_command(lines[0])
        self.assertEqual(name, "notice")
        self.assertEqual(sorted(properties), ["file", "title"])
        self.assertEqual(properties["file"], hostile)
        self.assertIn("svc has a structural boundary change", message)


# ---------------------------------------------------------------------------
# OBL-GLOBS-005: an exhausted structural budget stays exhausted
# ---------------------------------------------------------------------------

#: Three components, so that stickiness has somewhere to be observed. One is
#: not enough: with one candidate, a budget that reset per component would be
#: indistinguishable from one that did not.
STRUCTURAL_COMPONENTS = ("alpha", "beta", "gamma")

#: The detail the guard writes when it short-circuits before dispatch, as
#: distinct from the guardrail text the exhausting component carries.
ALREADY_EXHAUSTED = (
    "The aggregate structural-diff budget was already exhausted; "
    "no partial rows were retained"
)


class StructuralBudgetExhaustionIsStickyTests(unittest.TestCase):
    """OBL-GLOBS-005: after the first starves, the rest are not attempted."""

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario()
        for name in STRUCTURAL_COMPONENTS:
            cls.scene.component(
                name,
                path=name,
                provider="openapi-canonical",
                boundary=["contract.json"],
            )
            cls.scene.file(f"{name}/contract.json", _contract(1))
        cls.scene.commit("base")
        cls.base = _lock_and_commit(cls.scene, "lock base")
        for name in STRUCTURAL_COMPONENTS:
            cls.scene.file(f"{name}/contract.json", _contract(2))
        cls.scene.commit("target")
        cls.head = _lock_and_commit(cls.scene, "lock target")

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()

    def _run(self, *, rows: Optional[int], fmt: str = "json") -> Tuple[dict, int]:
        """Run one review, counting real calls into the provider's diff."""
        calls = []
        original = providers.OpenApiCanonicalProvider.structural_diff

        def counting(instance, before_ctx, after_ctx, budget):
            calls.append(instance)
            return original(instance, before_ctx, after_ctx, budget)

        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    providers.OpenApiCanonicalProvider, "structural_diff", counting
                )
            )
            if rows is not None:
                stack.enter_context(
                    mock.patch.object(
                        _provider_diff, "MAX_PROVIDER_DIFF_ROWS", rows
                    )
                )
            result = run_cli_in_process(
                self.scene.root,
                "review",
                f"{self.base}..{self.head}",
                "--format",
                fmt,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout), len(calls)

    def _review(self, *, rows: Optional[int]) -> Tuple[dict, int]:
        payload, calls = self._run(rows=rows)
        return payload["structural_changes"], calls

    @unittest.skipIf(jsonschema is None, "jsonschema is not installed")
    def test_a_starved_review_and_plan_still_match_their_committed_schemas(self):
        """OBL-GIT-SOURCE-073 as well: the one failure path review reaches.

        Every generated failure state in the schema class above makes review
        exit 2 with no payload, because an unreconciled endpoint is refused
        before a result is built. A starved structural budget is the state
        that does produce a review document containing `unavailable` rows, so
        it is the only place those two schemas meet a failure shape at all.
        """
        for stem, fmt in (("review", "json"), ("plan", "plan")):
            with self.subTest(schema=stem):
                payload, _calls = self._run(rows=0, fmt=fmt)
                schema = json.loads(
                    (SPEC / f"cli-output.{stem}.schema.json").read_text(
                        encoding="utf-8"
                    )
                )
                jsonschema.validate(payload, schema)

    def test_every_component_is_dispatched_when_the_budget_allows_it(self):
        """The premise: without it, "called once" could mean "never called"."""
        structural, calls = self._review(rows=None)
        self.assertEqual(calls, len(STRUCTURAL_COMPONENTS))
        self.assertIs(structural["complete"], True)
        self.assertIs(structural["truncated"], False)
        reports = {report["component"]: report for report in structural["reports"]}
        self.assertEqual(sorted(reports), sorted(STRUCTURAL_COMPONENTS))
        for name, report in reports.items():
            with self.subTest(component=name):
                self.assertEqual(report["status"], "complete")
                self.assertIs(report["complete"], True)
                self.assertIs(report["truncated"], False)
                self.assertTrue(report["documents"])

    def test_an_exhausted_budget_starves_every_later_component_without_dispatch(self):
        structural, calls = self._review(rows=0)
        self.assertEqual(calls, 1)
        self.assertIs(structural["complete"], False)
        self.assertIs(structural["truncated"], True)
        reports = {report["component"]: report for report in structural["reports"]}
        self.assertEqual(sorted(reports), sorted(STRUCTURAL_COMPONENTS))
        for name, report in reports.items():
            with self.subTest(component=name):
                self.assertEqual(report["status"], "unavailable")
                self.assertEqual(report["reason"], "limit-exceeded")
                self.assertIs(report["complete"], False)
                self.assertIs(report["truncated"], True)
                self.assertEqual(report["documents"], [])
                self.assertEqual(report["summary"], {"added": 0, "removed": 0, "changed": 0})

    def test_exactly_one_component_carries_the_guardrail_and_the_rest_the_guard(self):
        """Which is the evidence that the pre-dispatch guard is what ran.

        Three independent GuardrailErrors would also produce three
        limit-exceeded rows. Only the short-circuit produces two rows whose
        detail says the budget was *already* exhausted.
        """
        structural, _calls = self._review(rows=0)
        details = [report["detail"] for report in structural["reports"]]
        self.assertEqual(details.count(ALREADY_EXHAUSTED), 2)
        starved = [detail for detail in details if detail != ALREADY_EXHAUSTED]
        self.assertEqual(len(starved), 1)
        self.assertEqual(
            starved[0],
            "Structural boundary explanation exceeds the 0-row aggregate "
            "output limit. No partial structural result was emitted.",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
