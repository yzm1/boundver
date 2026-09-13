"""What a diagnostic reads before it answers, and what discovery refuses to guess.

Two subsystems meet in this file because they share a failure shape. When
`why` or `explain` resolves a diagnostic base it walks a repository's lock
history, and the only things stopping that walk from reading a hostile history
end to end are two constants nobody had ever asserted: 128 commits and 64 MiB.
When `discover` builds a component map it walks a repository's tracked names,
and the only things stopping that from allocating a tree-sized dictionary are
four more constants, three of which had only ever been exercised with the limit
monkeypatched down to 1 - a value at which `>= limit` and `> limit` are
indistinguishable, so the off-by-one at the real boundary was untested. In both
subsystems a breached bound is supposed to be disclosed rather than papered
over, and in both the disclosure is the part that had no test.

Getting the walk bound under a test needed a history whose lock file changes
without its component entry changing, because the walk stops at the first
mismatch and a fixture that let the entry drift would end after one read. The
trick is trailing newlines: JSON ignores them, so `boundary.lock.json` padded
with one more line feed per commit is a distinct blob at every commit and the
identical parsed document at all of them - and, unlike the obvious alternative
of an extra JSON key, it survives boundver's own strict lockfile validation, so
the same fixture works through the CLI. One hundred and thirty such commits
cost about seventeen seconds to build and are built once per class. The byte
budget needed no history at all, only patched constants, because the
interesting values are the exact size of one blob and that size plus one. The
discovery ceilings needed 1,000 and 50,000 tracked manifests, which nobody can
afford to create on disk; they are injected as a name stream in place of
`git ls-files`, and a premise test resolves a real three-manifest repository
both ways and requires the same component map, so the injection is shown to be
faithful before it is used at scale.

Manifest precedence needed a different kind of care. The declared order is
pinned by one assertion, `registered_manifest_specs() == DECLARED_MANIFESTS`,
and every behavioural check iterates the contract table rather than the tuple
it is testing - deriving the expected winner from the live tuple would have
made a reordered source agree with itself, which is the one mutation this
obligation exists to catch. The two order axes an earlier draft varied on disk
turned out to be incapable of varying anything: Git stores its index path
sorted, so staging or creating the manifests in reverse hands discovery a
byte-identical name list, which is now measured rather than assumed. The only
way to present the discovery loop with a differently ordered name list is the
injection seam, so that is where the index-order clause is answered, over a
fixture with two same-named directories where the order really does decide
which one keeps the plain name.

Three divergences are pinned rather than fixed. The byte budget can only
announce itself when the consumed total lands exactly on the limit, so ordinary
exhaustion is misreported as an unreadable lock; the current-commit fallback
makes the base equal the diff target, and `explain` then prints "No tracked
file changes detected" over a component that changed; and in a `--depth 1`
checkout - the default for actions/checkout, so the normal CI case - the walk
reaches the fetch boundary, calls it the commit that introduced the entry, and
diffs HEAD against itself. Each is marked `expectedFailure` and sits beside
tests that pin the current strings exactly, so a partial fix cannot pass
unnoticed. One obligation turned out to be partly stale: `boundver discover`
cannot run outside a Git repository at all, because the CLI refuses before
discovery is reached, so the end-to-end claim about a non-Git directory is
answered here against the fallback path itself rather than against a directory
the command will not accept.

The ceiling obligation asks for something stronger than a library-level
refusal: every one of the four must surface through `discover` *and* through
`init --discover` as exit 2 with the ceiling text on stderr and nothing on
stdout. Two of them reach that at their production constant - a name stream of
1,001 manifests, and one of 50,001 - and two cannot, because 50,000 real files
in one component directory and 200,000 filesystem entries are not fixtures
anyone can build. Those two run at a lowered constant, and a separate test
earns the production strings by observing each message at two different limits
and requiring the template to reproduce the table entry when the real constant
is substituted, so the four texts in `CEILINGS` are derivations rather than
transcriptions.

Covers OBL-GIT-SOURCE-128, OBL-GIT-SOURCE-129, OBL-GIT-SOURCE-130,
OBL-GIT-SOURCE-105, OBL-GIT-SOURCE-147 and OBL-GIT-SOURCE-148.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from unittest import mock

from boundver import _config, _discovery, _output
from boundver._config import (
    MAX_DISCOVERED_COMPONENTS,
    MAX_DISCOVERY_MANIFESTS,
    MAX_FILESYSTEM_TRAVERSAL_ENTRIES,
    MAX_PROVIDER_DETECTION_ENTRIES,
    _detect_provider,
    discover_components,
)
from boundver._lockfile import MAX_LOCKFILE_BYTES
from boundver._output import (
    _MAX_DIAGNOSTIC_LOCK_HISTORY_BYTES,
    _MAX_DIAGNOSTIC_LOCK_HISTORY_COMMITS,
    _component_lock_history_base,
    _resolve_lock_history_base,
)
from boundver._utils import GuardrailError

from tests._parity import run_cli, run_cli_in_process
from tests._scenarios import Scenario

LOCK = "boundary.lock.json"
CONFIG = "boundary.config.json"

#: The origin the resolver reports when it found the introducing commit. The
#: component is always named `svc` in this file, so the literal is complete.
PRECISE_ORIGIN = "commit that introduced the current lock entry for svc"

#: The four disclosed fallback origins, observed rather than guessed. Which
#: one comes out is the whole subject of the base-resolution tests below.
ROOT_COMMIT_LIMIT = (
    "root commit fallback (component lock history exceeded the commit limit)"
)
ROOT_BYTE_LIMIT = (
    "root commit fallback (component lock history exceeded the byte limit)"
)
CURRENT_BYTE_LIMIT = (
    "current commit fallback (component lock history exceeded the byte limit)"
)

#: What `explain` prints instead of a file list when the diff came back empty.
NO_CHANGES_LINE = "No tracked file changes detected for this component path."

#: How boundver spells a truncated history when it does say so. `review`
#: prints this on stderr from inside the shallow clone itself, which is what
#: makes the same word's absence from `why` and `explain` a contrast between
#: two commands on one repository rather than an unobservable claim.
SHALLOW_REMEDIATION = (
    "Repository is shallow; fetch complete history first "
    "(GitHub Actions: fetch-depth: 0; GitLab: GIT_DEPTH: 0)."
)

#: The non-Git approximation warning, verbatim, as one line on stderr.
FALLBACK_WARNING = (
    "WARNING: component discovery is using a bounded filesystem "
    "approximation because Git repository semantics are unavailable; "
    ".gitignore, nested ignore, and global-exclude behavior may differ."
)

#: The refusal that must replace that warning inside a real repository.
INDEX_REFUSAL = (
    "Component discovery could not read the Git index in a real repository; "
    "refusing a filesystem approximation"
)

#: The manifest contract as the obligation and docs/getting-started.md state
#: it: the declared precedence order, and the version field each kind maps to.
#: `test_the_registered_manifest_surface_is_the_one_this_table_describes`
#: reads the real tuple out of `discover_components` and requires this table to
#: match it exactly, so adding a fifth kind to the source fails here first and
#: every behavioural check below then covers it automatically.
DECLARED_MANIFESTS: Tuple[Tuple[str, Optional[str]], ...] = (
    ("package.json", "version"),
    ("pyproject.toml", "project.version"),
    ("Cargo.toml", "package.version"),
    ("go.mod", None),
)

#: A minimal well-formed body for each manifest kind, so a directory can hold
#: several at once and every one of them would be recognized on its own.
MANIFEST_BODIES: Dict[str, str] = {
    "package.json": '{"name": "example", "version": "1.0.0"}\n',
    "pyproject.toml": '[project]\nname = "example"\nversion = "1.0.0"\n',
    "Cargo.toml": '[package]\nname = "example"\nversion = "1.0.0"\n',
    "go.mod": "module example.com/example\n\ngo 1.21\n",
}

#: The four discovery ceilings: the constant that carries each one and the
#: message fragment its breach must produce. `max_discovery_manifests` has no
#: accept-side row of its own; see the test that explains why.
CEILINGS: Dict[str, Tuple[int, str]] = {
    "max_discovered_components": (
        MAX_DISCOVERED_COMPONENTS,
        "Component discovery guardrail exceeded: >1000 components",
    ),
    "max_discovery_manifests": (
        MAX_DISCOVERY_MANIFESTS,
        "Component discovery guardrail exceeded: >50000 manifests",
    ),
    "max_provider_detection_entries": (
        MAX_PROVIDER_DETECTION_ENTRIES,
        "Provider detection guardrail exceeded: >50000 available paths",
    ),
    "max_filesystem_traversal_entries": (
        MAX_FILESYSTEM_TRAVERSAL_ENTRIES,
        "Component discovery guardrail exceeded: "
        "filesystem traversal exceeds 200000 entries",
    ),
}

#: The provider ceiling carries a second production text, because the same
#: constant governs the filesystem crawl as well as the index-backed name set.
#: It has no row in `CEILINGS`, which is keyed by constant, so it is listed
#: here and derived alongside the other four.
PROVIDER_DIRECTORY_ENTRIES = (
    "Provider detection guardrail exceeded: >50000 directory entries"
)

#: The lowered limits the two unbuildable ceilings run at, and the pair of
#: limits every message is observed at twice so its template can be recovered.
#: Single digits that appear nowhere else in any of the five messages, so
#: replacing them recovers the template without touching the wording.
PATCHED_CEILING = 6
TEMPLATE_LIMITS = (3, 7)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _lock_history_repo(scene: Scenario, pads: int, *, drift: bool = True) -> dict:
    """Commit a lock, then rewrite it *pads* times without changing the entry.

    Each rewrite appends one more line feed. JSON ignores trailing whitespace,
    so every commit stores a distinct blob that parses to the identical
    document - which is exactly the shape the walk is specified against: the
    lock path changed, the component's entry did not, so the walk keeps going.

    Returns the locked `svc` entry, which is what the resolver compares
    against.
    """
    scene.component("svc", path="svc", boundary=["api"])
    scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
    scene.commit("one")
    generated = run_cli(scene.root, "generate", "--source", "head")
    if generated.returncode != 0:  # pragma: no cover - fixture failure
        raise AssertionError(f"generate failed: {generated.stderr}")
    scene.git("add", "--all")
    scene.git("commit", "-m", "lock")
    lock_path = scene.root / LOCK
    original = lock_path.read_bytes()
    for index in range(pads):
        lock_path.write_bytes(original + b"\n" * (index + 1))
        scene.git("commit", "-a", "-m", f"pad {index}")
    if drift:
        scene.append_line("svc/api/v1.yaml", "drift: true")
        scene.git("commit", "-a", "-m", "drift")
    return json.loads(original)["components"]["svc"]


def _lock_commits(scene: Scenario, revision: str = "HEAD") -> List[str]:
    """The first-parent commits that touched the lock, newest first."""
    output = scene.git("rev-list", "--first-parent", revision, "--", LOCK)
    return output.splitlines() if output else []


class _CountingCatBlob:
    """A pass-through around `_git_cat_blob` that records every read issued.

    The record is written before the read is delegated, because a read that
    breaches its own cap raises instead of returning - and that read was still
    issued, still spawned a Git process, and still counts against the work
    bound. `length` is None for those; nothing was consumed.
    """

    def __init__(self) -> None:
        self.real = _output._git_cat_blob
        self.calls: List[List] = []

    def __call__(self, repo_root, ref, *, max_bytes):
        record = [ref, max_bytes, None]
        self.calls.append(record)
        data = self.real(repo_root, ref, max_bytes=max_bytes)
        record[2] = len(data)
        return data

    @property
    def total_bytes(self) -> int:
        return sum(length for _ref, _cap, length in self.calls if length)


def _broken_root_lookup(repo_root, args, **kwargs):
    """Fail only the root-commit lookup, delegating every other Git call."""
    if args and args[0] == "rev-list" and "--max-parents=0" in args:
        raise subprocess.CalledProcessError(128, ["git", *args])
    return _REAL_GIT_RUN(repo_root, args, **kwargs)


def _malformed_root_lookup(repo_root, args, **kwargs):
    """Answer the root-commit lookup with something that is not an object ID."""
    if args and args[0] == "rev-list" and "--max-parents=0" in args:
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=0, stdout="not-an-object-id\n", stderr=""
        )
    return _REAL_GIT_RUN(repo_root, args, **kwargs)


_REAL_GIT_RUN = _output._git_run


def _why_json(root: Path, component: str = "svc") -> dict:
    """`why --format json` run inside this interpreter, so patches apply."""
    result = run_cli_in_process(
        root, "why", component, "--source", "head", "--format", "json"
    )
    payload = json.loads(result.stdout)
    payload["_returncode"] = result.returncode
    return payload


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-128: bounded work, and who gets blamed for the bound
# ---------------------------------------------------------------------------


class LockHistoryWalkBoundsTests(unittest.TestCase):
    """One base resolution reads at most 129 blobs and at most 64 MiB."""

    scene: Scenario

    #: The fixture pads the lock with one more line feed per commit, so the
    #: blob at the oldest lock commit is the unpadded document and every later
    #: one is that length plus its pad count. That makes the byte total the
    #: walk is allowed to spend predictable to the byte from the fixture alone.
    pads = _MAX_DIAGNOSTIC_LOCK_HISTORY_COMMITS + 1

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario("walkbounds")
        cls.locked = _lock_history_repo(cls.scene, cls.pads, drift=False)
        cls.all_lock_commits = _lock_commits(cls.scene)
        cls.unpadded_lock_bytes = len(
            cls.scene.blob(LOCK, cls.all_lock_commits[-1])
        )
        # One resolution, watched once. Every read is a `git cat-file`
        # subprocess, so re-running the walk per test would cost more than the
        # history did to build; the observations are shared instead.
        cls.counter = _CountingCatBlob()
        with mock.patch.object(_output, "_git_cat_blob", cls.counter):
            cls.answer = _resolve_lock_history_base(
                cls.scene.root,
                "head",
                None,
                LOCK,
                component_name="svc",
                locked_component=cls.locked,
            )

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()

    def test_the_history_holds_more_lock_commits_than_the_walk_is_allowed_to_read(self):
        """The premise: without this the bound would never be approached."""
        self.assertEqual(
            len(self.all_lock_commits), _MAX_DIAGNOSTIC_LOCK_HISTORY_COMMITS + 2
        )
        self.assertGreater(
            len(self.all_lock_commits), _MAX_DIAGNOSTIC_LOCK_HISTORY_COMMITS + 1
        )

    def test_the_read_counter_moves_with_the_history_it_is_pointed_at(self):
        """The premise for "at most 129": the counter is not stuck at a maximum.

        Pointing the same walk at a target with exactly 128 lock commits below
        it yields exactly 128 reads, so a count of 129 in the next test is a
        measurement rather than a ceiling the fixture happened to hit.
        """
        depth = _MAX_DIAGNOSTIC_LOCK_HISTORY_COMMITS
        target = self.all_lock_commits[len(self.all_lock_commits) - depth]
        counter = _CountingCatBlob()
        with mock.patch.object(_output, "_git_cat_blob", counter):
            answer = _component_lock_history_base(
                self.scene.root, target, LOCK, "svc", self.locked
            )
        self.assertEqual(len(counter.calls), depth)
        self.assertEqual(answer, (self.all_lock_commits[-1], PRECISE_ORIGIN))

    def test_one_base_resolution_issues_at_most_one_hundred_twenty_nine_reads(self):
        self.assertEqual(
            len(self.counter.calls), _MAX_DIAGNOSTIC_LOCK_HISTORY_COMMITS + 1
        )
        self.assertLessEqual(
            len(self.counter.calls), _MAX_DIAGNOSTIC_LOCK_HISTORY_COMMITS + 1
        )
        self.assertEqual(
            len({ref for ref, _cap, _length in self.counter.calls}),
            len(self.counter.calls),
            "no commit is read twice",
        )

    def test_every_read_asks_for_no_more_than_the_budget_that_remains(self):
        """Recompute the accounting from the observed reads and compare.

        The oracle is independent of the source: `remaining` is derived only
        from the lengths this test watched come back, and the cap the code
        asked for is compared against it. An accounting bug that let
        `consumed_bytes` drift would show up as a mismatch here even though
        every individual read stayed under the per-lockfile cap.

        Under the production budget the remainder never falls below
        `MAX_LOCKFILE_BYTES`, so every cap here comes out the same number. The
        half of the oracle that discriminates - a cap that shrinks with the
        remainder - needs a squeezed budget, and is checked by
        `ByteBudgetAttributionTests.test_the_cap_shrinks_with_the_budget`.
        """
        consumed = 0
        for index, (ref, cap, length) in enumerate(self.counter.calls):
            with self.subTest(read=index, ref=ref):
                expected = min(
                    MAX_LOCKFILE_BYTES,
                    _MAX_DIAGNOSTIC_LOCK_HISTORY_BYTES - consumed,
                )
                self.assertEqual(cap, expected)
                self.assertIsNotNone(length)
                self.assertLessEqual(length, cap)
            consumed += length or 0

    def test_the_fixture_predicts_every_blob_length_the_walk_may_read(self):
        """The premise for the byte total: the fixture's sizes are known.

        Git stores what the fixture wrote, so the blob at the k-th pad commit
        is the unpadded lock plus k line feeds. Reading those lengths back out
        of the repository and comparing them against that arithmetic is what
        turns the next test's total into an exact expectation instead of a
        number compared against a limit five hundred times larger.
        """
        newest_first = [
            len(self.scene.blob(LOCK, commit)) for commit in self.all_lock_commits
        ]
        self.assertEqual(
            newest_first,
            [self.unpadded_lock_bytes + pad for pad in range(self.pads, -1, -1)],
        )
        self.assertEqual(len(set(newest_first)), len(newest_first))

    def test_the_walk_never_reads_more_blob_bytes_than_the_budget_allows(self):
        """The budget clause, against a total the fixture fixes exactly.

        `_MAX_DIAGNOSTIC_LOCK_HISTORY_BYTES` is five hundred times larger than
        anything this history can spend, so comparing the total against it
        alone would pass under any accounting fault. The discriminating
        expectation is the one the fixture supports: the walk reads the 129
        newest lock blobs, whose lengths the previous test derived, and
        therefore spends exactly their sum - no blob read twice, none skipped,
        and the oldest commit in the history never touched at all.
        """
        read_count = _MAX_DIAGNOSTIC_LOCK_HISTORY_COMMITS + 1
        expected_lengths = [
            self.unpadded_lock_bytes + pad
            for pad in range(self.pads, self.pads - read_count, -1)
        ]
        self.assertEqual(
            [length for _ref, _cap, length in self.counter.calls],
            expected_lengths,
        )
        self.assertEqual(self.counter.total_bytes, sum(expected_lengths))
        self.assertNotIn(self.unpadded_lock_bytes, expected_lengths)
        for length in expected_lengths:
            self.assertLess(length, MAX_LOCKFILE_BYTES)
        self.assertGreater(self.counter.total_bytes, 0)
        self.assertLessEqual(
            self.counter.total_bytes, _MAX_DIAGNOSTIC_LOCK_HISTORY_BYTES
        )

    def test_exhausting_the_commit_limit_is_disclosed_as_the_commit_limit(self):
        """And the base it falls back to is the repository's root commit."""
        base, origin = self.answer
        self.assertEqual(origin, ROOT_COMMIT_LIMIT)
        self.assertEqual(
            base, self.scene.git("rev-list", "--max-parents=0", "HEAD")
        )

    def test_the_documented_constants_are_the_numbers_the_contract_names(self):
        self.assertEqual(_MAX_DIAGNOSTIC_LOCK_HISTORY_COMMITS, 128)
        self.assertEqual(_MAX_DIAGNOSTIC_LOCK_HISTORY_BYTES, 64 * 1024 * 1024)
        self.assertEqual(_MAX_DIAGNOSTIC_LOCK_HISTORY_BYTES, 67_108_864)


class ByteBudgetAttributionTests(unittest.TestCase):
    """OBL-GIT-SOURCE-128: who gets blamed when the byte budget runs out."""

    scene: Scenario

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario("bytebudget")
        cls.locked = _lock_history_repo(cls.scene, 3)
        cls.newest_lock_bytes = len(cls.scene.blob(LOCK))

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()

    def _resolve_with_budget(self, budget: int):
        with mock.patch.object(
            _output, "_MAX_DIAGNOSTIC_LOCK_HISTORY_BYTES", budget
        ):
            return _resolve_lock_history_base(
                self.scene.root,
                "head",
                None,
                LOCK,
                component_name="svc",
                locked_component=self.locked,
            )

    def test_the_unpatched_walk_finds_a_precise_base(self):
        """The premise: this history resolves precisely when nothing is squeezed."""
        base, origin = self._resolve_with_budget(
            _MAX_DIAGNOSTIC_LOCK_HISTORY_BYTES
        )
        self.assertEqual(origin, PRECISE_ORIGIN)
        self.assertNotEqual(base, self.scene.head())

    def test_the_fixture_holds_several_distinct_lock_blobs(self):
        """The premise: the walk has more than one blob to spend a budget on."""
        commits = _lock_commits(self.scene)
        blobs = [self.scene.blob(LOCK, commit) for commit in commits]
        self.assertEqual(len(commits), 4)
        self.assertEqual(len(set(blobs)), 4)
        parsed = {json.dumps(json.loads(blob), sort_keys=True) for blob in blobs}
        self.assertEqual(len(parsed), 1, "the lock document itself must not move")

    def test_a_budget_that_lands_exactly_on_zero_names_the_byte_limit(self):
        """The premise for the divergence: the message is reachable, just barely.

        With the budget set to the exact length of the first blob the walk
        reads, the next iteration finds `remaining == 0` and the byte-limit
        string is what comes out. That is the only arrangement that produces
        it, which is the finding.
        """
        base, origin = self._resolve_with_budget(self.newest_lock_bytes)
        self.assertEqual(origin, ROOT_BYTE_LIMIT)
        self.assertEqual(
            base, self.scene.git("rev-list", "--max-parents=0", "HEAD")
        )

    def test_the_cap_shrinks_with_the_budget(self):
        """The discriminating half of the accounting oracle.

        With one byte of budget left over after the first read, the second read
        must ask for exactly one byte. Recomputing the remainder from the
        lengths observed coming back gives the expected cap without consulting
        the source, so a `consumed_bytes` that stopped accumulating would show
        up as a cap that never moved.
        """
        counter = _CountingCatBlob()
        budget = self.newest_lock_bytes + 1
        with (
            mock.patch.object(
                _output, "_MAX_DIAGNOSTIC_LOCK_HISTORY_BYTES", budget
            ),
            mock.patch.object(_output, "_git_cat_blob", counter),
        ):
            _resolve_lock_history_base(
                self.scene.root,
                "head",
                None,
                LOCK,
                component_name="svc",
                locked_component=self.locked,
            )
        caps = [cap for _ref, cap, _length in counter.calls]
        lengths = [length for _ref, _cap, length in counter.calls]
        self.assertEqual(caps, [budget, 1])
        self.assertEqual(lengths, [self.newest_lock_bytes, None])
        self.assertEqual(len(set(caps)), 2, "the cap has to move at all")
        consumed = 0
        for index, (_ref, cap, length) in enumerate(counter.calls):
            with self.subTest(read=index):
                self.assertEqual(cap, min(MAX_LOCKFILE_BYTES, budget - consumed))
            consumed += length or 0

    def test_a_budget_exhausted_one_byte_late_is_attributed_to_the_budget(self):
        _base, origin = self._resolve_with_budget(self.newest_lock_bytes + 1)
        self.assertEqual(origin, ROOT_BYTE_LIMIT)

    def test_a_walk_stopped_by_the_byte_budget_says_so(self):
        """A short bounded read is identified as exhaustion, not corruption."""
        _base, origin = self._resolve_with_budget(self.newest_lock_bytes + 1)
        self.assertEqual(origin, ROOT_BYTE_LIMIT)


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-129: the last two rungs of the fallback ladder
# ---------------------------------------------------------------------------


class RootAndCurrentCommitFallbackTests(unittest.TestCase):
    """`_root_commit_fallback` has two outcomes and neither had a test."""

    scene: Scenario

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario("fallbackladder")
        cls.locked = _lock_history_repo(cls.scene, 3)
        cls.newest_lock_bytes = len(cls.scene.blob(LOCK))
        cls.root_commit = cls.scene.git("rev-list", "--max-parents=0", "HEAD")
        cls.head = cls.scene.head()

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()

    def _squeezed(self):
        """Patch the byte budget to the exact landing that reaches the fallback."""
        return mock.patch.object(
            _output,
            "_MAX_DIAGNOSTIC_LOCK_HISTORY_BYTES",
            self.newest_lock_bytes,
        )

    def test_the_root_commit_really_is_older_than_the_diff_target(self):
        """The premise: the root branch can only be checked on a real history."""
        self.assertNotEqual(self.root_commit, self.head)
        ancestry = self.scene.first_parent_commits()
        self.assertIn(self.root_commit, ancestry)
        self.assertEqual(ancestry[-1], self.root_commit)
        self.assertEqual(ancestry[0], self.head)

    def test_the_root_branch_returns_a_validated_root_commit(self):
        with self._squeezed():
            base, origin = _resolve_lock_history_base(
                self.scene.root,
                "head",
                None,
                LOCK,
                component_name="svc",
                locked_component=self.locked,
            )
        self.assertEqual(origin, ROOT_BYTE_LIMIT)
        self.assertEqual(base, self.root_commit)
        self.assertEqual(len(base), 40)
        self.assertTrue(set(base) <= set("0123456789abcdef"))

    def test_the_root_branch_diffs_a_base_older_than_the_target(self):
        """Clause (a): a strictly older base, and a non-empty change set."""
        with self._squeezed():
            payload = _why_json(self.scene.root)
        self.assertEqual(payload["_returncode"], 1)
        self.assertEqual(payload["diagnostic_base"], self.root_commit)
        self.assertEqual(payload["diagnostic_base_origin"], ROOT_BYTE_LIMIT)
        self.assertNotEqual(payload["diagnostic_base"], self.head)
        self.assertEqual(payload["changed_files_status"], "ok")
        self.assertEqual(
            [entry["path"] for entry in payload["changed_files"]],
            ["svc/api/v1.yaml"],
        )

    def test_the_root_branch_lists_the_changed_file_in_explain_too(self):
        """The premise for the current-commit pin: explain can list a file."""
        with self._squeezed():
            result = run_cli_in_process(
                self.scene.root, "explain", "svc", "--source", "head"
            )
        self.assertEqual(result.returncode, 0)
        self.assertIn("Changed files (1):", result.stdout)
        self.assertIn("svc/api/v1.yaml", result.stdout)
        self.assertNotIn(NO_CHANGES_LINE, result.stdout)

    def test_a_root_lookup_that_fails_falls_through_to_the_diff_target(self):
        with self._squeezed(), mock.patch.object(
            _output, "_git_run", _broken_root_lookup
        ):
            base, origin = _resolve_lock_history_base(
                self.scene.root,
                "head",
                None,
                LOCK,
                component_name="svc",
                locked_component=self.locked,
            )
        self.assertEqual(origin, CURRENT_BYTE_LIMIT)
        self.assertEqual(base, self.head)

    def test_a_root_lookup_that_answers_garbage_falls_through_too(self):
        """The object-ID check is what stands between an answer and another argv."""
        with self._squeezed(), mock.patch.object(
            _output, "_git_run", _malformed_root_lookup
        ):
            base, origin = _resolve_lock_history_base(
                self.scene.root,
                "head",
                None,
                LOCK,
                component_name="svc",
                locked_component=self.locked,
            )
        self.assertEqual(origin, CURRENT_BYTE_LIMIT)
        self.assertEqual(base, self.head)

    def test_the_current_commit_branch_is_distinguishable_in_why_json(self):
        """Clause (b), first half: the JSON view names the rung it landed on."""
        with self._squeezed(), mock.patch.object(
            _output, "_git_run", _broken_root_lookup
        ):
            payload = _why_json(self.scene.root)
        self.assertEqual(payload["_returncode"], 1)
        self.assertEqual(payload["diagnostic_base_origin"], CURRENT_BYTE_LIMIT)
        self.assertEqual(payload["diagnostic_base"], self.head)
        self.assertNotEqual(payload["diagnostic_base_origin"], ROOT_BYTE_LIMIT)
        self.assertNotIn(PRECISE_ORIGIN, payload["diagnostic_base_origin"])

    def test_the_current_commit_branch_discloses_that_no_diff_was_run(self):
        with self._squeezed(), mock.patch.object(
            _output, "_git_run", _broken_root_lookup
        ):
            payload = _why_json(self.scene.root)
            explain = run_cli_in_process(
                self.scene.root, "explain", "svc", "--source", "head"
            )
            why_text = run_cli_in_process(
                self.scene.root, "why", "svc", "--source", "head"
            )
        self.assertEqual(payload["changed_files_status"], "not-run")
        self.assertEqual(payload["changed_files"], [])
        self.assertEqual(explain.returncode, 0)
        self.assertNotIn(NO_CHANGES_LINE, explain.stdout)
        self.assertIn("only available fallback base is the current commit", explain.stdout)
        self.assertEqual(why_text.returncode, 1)
        self.assertIn("Status: DRIFTED", why_text.stdout)
        self.assertIn(
            "Changed-file diagnostics were not run for source=head.",
            why_text.stdout,
        )

    def test_the_current_commit_branch_is_never_rendered_as_no_changes(self):
        """An unavailable comparison is not rendered as an empty comparison."""
        with self._squeezed(), mock.patch.object(
            _output, "_git_run", _broken_root_lookup
        ):
            explain = run_cli_in_process(
                self.scene.root, "explain", "svc", "--source", "head"
            )
        self.assertNotIn(NO_CHANGES_LINE, explain.stdout)


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-130: the base a shallow checkout can actually support
# ---------------------------------------------------------------------------


class ShallowCheckoutBaseTests(unittest.TestCase):
    """A `--depth 1` clone is the CI default, and the walk cannot see past it."""

    scene: Scenario
    directory: tempfile.TemporaryDirectory

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario("shallowbase")
        _lock_history_repo(cls.scene, 3)
        cls.directory = tempfile.TemporaryDirectory()
        cls.clone = Path(cls.directory.name) / "shallow"
        cloned = subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--no-local",
                cls.scene.root.resolve().as_uri(),
                str(cls.clone),
            ],
            capture_output=True,
            text=True,
        )
        cls.clone_stderr = cloned.stderr
        cls.clone_returncode = cloned.returncode

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()
        cls.scene.close()

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.clone), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def _clone_why(self) -> dict:
        result = run_cli(
            self.clone, "why", "svc", "--source", "head", "--format", "json"
        )
        payload = json.loads(result.stdout)
        payload["_returncode"] = result.returncode
        return payload

    def test_the_clone_really_is_shallow_and_the_source_really_is_not(self):
        """The premise: without a truncated fetch there is nothing to disclose."""
        self.assertEqual(self.clone_returncode, 0, self.clone_stderr)
        self.assertEqual(self._git("rev-parse", "--is-shallow-repository"), "true")
        self.assertEqual(
            self.scene.git("rev-parse", "--is-shallow-repository"), "false"
        )
        self.assertEqual(self._git("rev-parse", "HEAD"), self.scene.head())
        self.assertEqual(self._git("rev-list", "--count", "HEAD"), "1")

    def test_the_fetch_boundary_is_what_the_root_lookup_returns(self):
        """`--max-parents=0` in a depth-1 clone answers HEAD, not the real root."""
        boundary = self._git(
            "rev-list", "--first-parent", "--max-parents=0", "HEAD"
        )
        self.assertEqual(boundary, self.scene.head())
        self.assertNotEqual(
            boundary, self.scene.git("rev-list", "--max-parents=0", "HEAD")
        )

    def test_the_walk_consumed_the_whole_fetched_history(self):
        """The premise for the disclosure clause: nothing was left unexamined."""
        fetched = self._git("rev-list", "--first-parent", "HEAD", "--", LOCK)
        self.assertEqual(fetched.splitlines(), [self.scene.head()])
        self.assertEqual(len(_lock_commits(self.scene)), 4)

    def test_the_full_checkout_resolves_a_base_older_than_head(self):
        """The contrast the shallow answer is measured against."""
        result = run_cli(
            self.scene.root, "why", "svc", "--source", "head", "--format", "json"
        )
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(payload["diagnostic_base_origin"], PRECISE_ORIGIN)
        self.assertNotEqual(payload["diagnostic_base"], self.scene.head())
        self.assertEqual(
            [entry["path"] for entry in payload["changed_files"]],
            ["svc/api/v1.yaml"],
        )

    def test_a_shallow_checkout_matches_the_full_one_or_discloses_truncation(self):
        """A fetch boundary is disclosed instead of claimed as precise history."""
        full = json.loads(
            run_cli(
                self.scene.root,
                "why",
                "svc",
                "--source",
                "head",
                "--format",
                "json",
            ).stdout
        )
        shallow = self._clone_why()
        if shallow["diagnostic_base"] != full["diagnostic_base"]:
            self.assertNotEqual(shallow["diagnostic_base_origin"], PRECISE_ORIGIN)

    def test_another_command_discloses_the_truncation_in_this_same_clone(self):
        """The premise for the silence below: boundver can say it, and does.

        Asked for a base one commit back, `review` inside this very clone
        refuses and names the truncation, with the remediation a CI user needs.
        That is the standard the obligation measures `why` and `explain`
        against, and observing it here is what makes their silence a contrast
        between two commands on one repository rather than an absence no
        mechanism could ever have produced.
        """
        result = run_cli(self.clone, "review", "--base", "HEAD~1", "--target", "HEAD")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(SHALLOW_REMEDIATION, result.stderr)
        self.assertIn("shallow", result.stderr.lower())

    def test_the_shallow_fallback_is_explicit_and_still_lists_evidence(self):
        shallow = self._clone_why()
        self.assertEqual(shallow["_returncode"], 1)
        self.assertEqual(shallow["diagnostic_base"], self.scene.head())
        self.assertIn("shallow", shallow["diagnostic_base_origin"])
        self.assertNotEqual(shallow["diagnostic_base_origin"], PRECISE_ORIGIN)
        self.assertEqual(shallow["changed_files_status"], "ok")
        self.assertTrue(shallow["changed_files"])
        why = run_cli(
            self.clone, "why", "svc", "--source", "head", "--format", "json"
        )
        explain = run_cli(self.clone, "explain", "svc", "--source", "head")
        self.assertEqual(explain.returncode, 0)
        self.assertNotIn(NO_CHANGES_LINE, explain.stdout)
        self.assertIn("shallow", why.stdout.lower())
        self.assertIn("shallow", explain.stdout.lower())
        self.assertIn("fetch complete history", explain.stdout.lower())


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-105: which stream the approximation warning goes to
# ---------------------------------------------------------------------------


def _tracked_names(names):
    """Stand in for `git ls-files`, yielding exactly *names*."""

    def fake(repo_root, args):
        yield from names

    return fake


def _git_index_unavailable(*_args, **_kwargs):
    raise subprocess.CalledProcessError(128, ["git", "ls-files"])


class DiscoveryWarningChannelTests(unittest.TestCase):
    """The warning belongs on stderr; a real repository gets a refusal instead."""

    def _repository(self) -> Scenario:
        scene = Scenario("warningchannel")
        scene.component("svc", path="svc", provider="leaf")
        scene.file("svc/package.json", MANIFEST_BODIES["package.json"])
        scene.commit()
        return scene

    def test_the_cli_never_reaches_discovery_outside_a_repository(self):
        """The obligation's non-Git directory is unreachable through the CLI.

        `boundver discover` resolves the repository root before dispatching, so
        outside a work tree it exits 2 with a plain-text error and prints no
        JSON at all. That is why the channel test below reaches the fallback by
        making Git's index unavailable inside a repository instead.
        """
        with tempfile.TemporaryDirectory() as outside:
            root = Path(outside)
            (root / "svc").mkdir()
            (root / "svc" / "package.json").write_text(
                MANIFEST_BODIES["package.json"], encoding="utf-8"
            )
            result = run_cli(root, "discover", "--format", "json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("ERROR: Not inside a git repository.", result.stderr)

    def test_the_fallback_warning_is_the_line_these_tests_look_for(self):
        """The premise: the warning really is emitted when the fallback runs."""
        with self._repository() as scene:
            with (
                mock.patch.object(
                    _discovery, "_iter_bounded_git_paths", _git_index_unavailable
                ),
                mock.patch.object(
                    _discovery, "_is_git_repository", return_value=False
                ),
            ):
                result = run_cli_in_process(
                    scene.root, "discover", "--format", "json"
                )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr.strip(), FALLBACK_WARNING)

    def test_discover_json_keeps_one_document_on_stdout(self):
        with self._repository() as scene:
            with (
                mock.patch.object(
                    _discovery, "_iter_bounded_git_paths", _git_index_unavailable
                ),
                mock.patch.object(
                    _discovery, "_is_git_repository", return_value=False
                ),
            ):
                result = run_cli_in_process(
                    scene.root, "discover", "--format", "json"
                )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(sorted(payload["components"]), ["svc"])
        self.assertEqual(
            payload["components"]["svc"]["version_source"],
            {"file": "package.json", "field": "version"},
        )
        self.assertNotIn("WARNING", result.stdout)
        self.assertEqual(result.stdout.count("\n{"), 0)

    def test_the_stdout_check_would_fail_if_the_warning_shared_the_stream(self):
        """The premise for that assertion: `json.loads` is a real detector.

        A warning printed with a bare `print()` would land in front of the
        document, and the parse would stop on the first character. Showing that
        here keeps the assertion above from being an absence nobody could have
        observed.
        """
        with self._repository() as scene:
            with (
                mock.patch.object(
                    _discovery, "_iter_bounded_git_paths", _git_index_unavailable
                ),
                mock.patch.object(
                    _discovery, "_is_git_repository", return_value=False
                ),
            ):
                result = run_cli_in_process(
                    scene.root, "discover", "--format", "json"
                )
        polluted = result.stderr + result.stdout
        with self.assertRaises(json.JSONDecodeError):
            json.loads(polluted)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(result.stdout + result.stdout)

    def test_a_repository_whose_index_cannot_be_read_refuses_to_approximate(self):
        with self._repository() as scene:
            (scene.root / ".git" / "index").write_bytes(b"not an index\n")
            result = run_cli(scene.root, "discover", "--format", "json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("ERROR: component discovery failed:", result.stderr)
        self.assertIn(INDEX_REFUSAL, result.stderr)
        self.assertNotIn(FALLBACK_WARNING, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_the_same_repository_discovers_the_component_before_corruption(self):
        """The premise: the corrupt index is what caused the refusal."""
        with self._repository() as scene:
            result = run_cli(scene.root, "discover", "--format", "json")
            payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(sorted(payload["components"]), ["svc"])


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-147: which manifest wins, and what it maps to
# ---------------------------------------------------------------------------


def registered_manifest_specs() -> Tuple[Tuple[str, Optional[str]], ...]:
    """The manifest surface, read out of `discover_components` at runtime.

    `manifest_specs` is a local inside the function rather than a module
    constant, so there is nothing to import; the tuple survives as a code
    constant. Exactly one test consumes it - the bridge that requires it to
    equal `DECLARED_MANIFESTS` - and every behavioural check below is driven
    by the contract table instead. That division is deliberate: a pairing test
    that derived its expected winner from this tuple would agree with a
    reordered source, which is the mutation the obligation is about. A fifth
    manifest kind added to the source is caught by the bridge and nowhere
    else, which is the signal to widen the contract table by hand.
    """
    candidates = [
        const
        for const in _discovery.discover_components.__code__.co_consts
        if isinstance(const, tuple)
        and const
        and all(
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            and (item[1] is None or isinstance(item[1], str))
            for item in const
        )
    ]
    if len(candidates) != 1:  # pragma: no cover - shape change, not a behaviour
        raise AssertionError(
            "expected exactly one manifest-spec constant in "
            f"discover_components, found {len(candidates)}: {candidates!r}"
        )
    return candidates[0]


class ManifestPrecedenceTests(unittest.TestCase):
    """One manifest wins a polyglot directory, and it is the declared first."""

    #: A boundary artifact, so the component's path list is something a losing
    #: manifest could leak into rather than a list already known to be empty.
    OPENAPI = ("openapi.yaml", "openapi: 3.1.0\n")

    def _build(self, scene, names, *, extra=(), reverse_creation=False) -> None:
        """Write and stage one polyglot directory in the requested order."""
        order = list(reversed(names)) if reverse_creation else list(names)
        scene.component("svc", path="svc", provider="leaf")
        for name in order:
            scene.file(f"svc/{name}", MANIFEST_BODIES[name])
        for name, body in extra:
            scene.file(f"svc/{name}", body)
        scene.write_config()
        for name in order:
            scene.git("add", f"svc/{name}")
        for name, _body in extra:
            scene.git("add", f"svc/{name}")
        scene.git("add", CONFIG)
        scene.git("commit", "-m", "manifests")

    def _discover(self, names, *, extra=(), reverse_creation=False) -> Dict[str, dict]:
        """Put every manifest in *names* in one directory and discover it."""
        with Scenario("manifests") as scene:
            self._build(
                scene, names, extra=extra, reverse_creation=reverse_creation
            )
            return discover_components(scene.root)

    def test_the_registered_manifest_surface_is_the_one_this_table_describes(self):
        """A kind added to the source has to be added to the contract too.

        This is the one place the source tuple is read, and every behavioural
        test below is driven by `DECLARED_MANIFESTS` instead, so the declared
        order is pinned here and only here.
        """
        self.assertEqual(registered_manifest_specs(), DECLARED_MANIFESTS)

    def test_the_index_hands_discovery_one_order_whatever_order_files_arrived(self):
        """Measured, because it is why an on-disk order axis discriminates.

        Git stores its index sorted by path, so `ls-files` answers with the
        same sequence whether the manifests were written and staged in the
        declared order or the reverse. An earlier draft ran the precedence
        table four times over that unvarying input; this measurement is what
        replaced two of those axes, and the reversed-stream test below is
        where the index-order clause is actually answered against an input
        that differs.
        """
        names = [manifest for manifest, _field in DECLARED_MANIFESTS]
        listings = []
        for reverse_creation in (False, True):
            with Scenario("manifest-order") as scene:
                self._build(scene, names, reverse_creation=reverse_creation)
                listings.append(scene.git("ls-files").splitlines())
        self.assertEqual(listings[0], listings[1])
        self.assertEqual(listings[0], sorted(listings[0]))
        self.assertNotEqual(
            [entry for entry in listings[0] if entry.startswith("svc/")],
            [f"svc/{name}" for name in names],
            "path order must differ from the declared order, or the reversal "
            "would be indistinguishable from it",
        )

    def test_a_reversed_index_order_still_names_the_same_components(self):
        """The index-order clause, over an input that really does differ.

        No on-disk arrangement can vary the sequence discovery consumes, so
        the only honest way to ask this is through the name-stream seam that
        `DiscoveryCeilingTests.test_the_injected_name_stream_answers_like_the
        _real_index` shows is faithful. The fixture is two directories sharing
        a basename, because that is where order decides an outcome: whichever
        of `a/svc` and `b/svc` the loop reaches first keeps the plain name and
        the other becomes `svc-2`. Removing the `sorted()` from the manifest
        loop makes the reversed stream swap those two paths, which is what
        gives this test teeth the staging-order axis never had.
        """
        with Scenario("index-order") as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("a/svc/package.json", MANIFEST_BODIES["package.json"])
            scene.file("b/svc/package.json", MANIFEST_BODIES["package.json"])
            scene.file("c/api/pyproject.toml", MANIFEST_BODIES["pyproject.toml"])
            scene.commit()
            names = scene.git("ls-files").splitlines()
            backward = list(reversed(names))
            real = discover_components(scene.root)
            with mock.patch.object(
                _discovery, "_iter_bounded_git_paths", _tracked_names(names)
            ):
                forward_order = discover_components(scene.root)
            with mock.patch.object(
                _discovery, "_iter_bounded_git_paths", _tracked_names(backward)
            ):
                reverse_order = discover_components(scene.root)
        self.assertNotEqual(names, backward, "the axis has to vary its input")
        self.assertEqual(
            {name: record["path"] for name, record in real.items()},
            {"svc": "a/svc", "svc-2": "b/svc", "api": "c/api"},
        )
        self.assertEqual(forward_order, real)
        self.assertEqual(reverse_order, real)

    def test_each_manifest_on_its_own_sets_the_version_source_it_declares(self):
        """The premise for "the losers contribute nothing": each one can win."""
        for manifest, field in DECLARED_MANIFESTS:
            with self.subTest(manifest=manifest):
                found = self._discover([manifest])
                self.assertEqual(sorted(found), ["svc"])
                expected = (
                    None if field is None else {"file": manifest, "field": field}
                )
                self.assertEqual(found["svc"]["version_source"], expected)
                self.assertEqual(found["svc"]["path"], "svc")

    def test_every_ordered_pair_is_won_by_the_earlier_declared_kind(self):
        """Six pairs, each in both creation orders, against the contract.

        The expectation comes from `DECLARED_MANIFESTS` and not from the tuple
        read out of `discover_components`, which is the whole point. Deriving
        the winner from the tuple under test makes a reordered source agree
        with itself: swapping pyproject.toml and Cargo.toml there - the
        reorder this obligation names as its headline risk - leaves every pair
        green. Against the contract that swap fails here.
        """
        pairs = [
            (DECLARED_MANIFESTS[first], DECLARED_MANIFESTS[second])
            for first in range(len(DECLARED_MANIFESTS))
            for second in range(first + 1, len(DECLARED_MANIFESTS))
        ]
        self.assertEqual(len(pairs), 6)
        for (winner, field), (loser, _loser_field) in pairs:
            for creation in ([winner, loser], [loser, winner]):
                with self.subTest(winner=winner, loser=loser, created=creation):
                    found = self._discover(creation)
                    self.assertEqual(sorted(found), ["svc"])
                    expected = (
                        None if field is None else {"file": winner, "field": field}
                    )
                    self.assertEqual(found["svc"]["version_source"], expected)

    def test_the_losing_manifests_contribute_nothing_at_all(self):
        """The whole record, compared against the winner standing alone.

        An earlier draft ended by asserting that no manifest name appeared in
        the boundary path list, four lines after asserting that list was
        empty. The discriminating comparison is against a directory holding
        only the winner: anything a loser might have contributed - a second
        component, a merged version_source, an extra boundary path - makes the
        two records differ. The second shape adds an `openapi.yaml`, so the
        boundary selector is a real non-empty list a manifest could leak into
        rather than a container already known to hold nothing.
        """
        names = [manifest for manifest, _field in DECLARED_MANIFESTS]
        first, field = DECLARED_MANIFESTS[0]
        expected_source = (
            None if field is None else {"file": first, "field": field}
        )
        shapes = {
            "implicit boundary": ((), {"provider": "implicit", "paths": []}),
            "openapi boundary": (
                (self.OPENAPI,),
                {"provider": "openapi", "paths": ["openapi.yaml"]},
            ),
        }
        for label, (extra, boundary) in shapes.items():
            with self.subTest(shape=label):
                alone = self._discover([first], extra=extra)
                polyglot = self._discover(names, extra=extra)
                self.assertEqual(
                    alone,
                    {
                        "svc": {
                            "path": "svc",
                            "version_source": expected_source,
                            "boundary": boundary,
                        }
                    },
                )
                self.assertEqual(polyglot, alone)
                self.assertEqual(len(polyglot), 1)
                self.assertEqual(
                    polyglot["svc"]["boundary"]["paths"], boundary["paths"]
                )


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-148: ceilings refuse, and refuse at the right number
# ---------------------------------------------------------------------------


def _manifest_stream(count: int):
    """A tracked-name stream of *count* manifests, one per directory."""
    return _tracked_names(f"d{index:06d}/package.json" for index in range(count))


@contextlib.contextmanager
def _synthetic_manifest_index(count: int):
    """Expose a large logical index without allocating its files on disk."""
    with (
        mock.patch.object(
            _discovery,
            "_iter_bounded_git_paths",
            _manifest_stream(count),
        ),
        mock.patch.object(_discovery, "_manifest_available", return_value=True),
    ):
        yield


def _guardrail_message(call, *args, **kwargs) -> str:
    """Run *call*, require a GuardrailError, and return its exact message."""
    try:
        call(*args, **kwargs)
    except GuardrailError as exc:
        return str(exc)
    raise AssertionError(f"{call.__name__} produced no ceiling breach")


def _ceiling_repository(name: str) -> Scenario:
    """One declared component, committed - the tree every ceiling starts from."""
    scene = Scenario(name)
    scene.component("svc", path="svc", provider="leaf")
    scene.file("svc/main.py", "x = 1\n")
    scene.commit()
    return scene


def _components_message(limit: int) -> str:
    with _ceiling_repository("template-components") as scene:
        with (
            mock.patch.object(_config, "MAX_DISCOVERED_COMPONENTS", limit),
            _synthetic_manifest_index(limit + 1),
        ):
            return _guardrail_message(discover_components, scene.root)


def _manifests_message(limit: int) -> str:
    with _ceiling_repository("template-manifests") as scene:
        with (
            mock.patch.object(_config, "MAX_DISCOVERY_MANIFESTS", limit),
            _synthetic_manifest_index(limit + 1),
        ):
            return _guardrail_message(discover_components, scene.root)


def _available_paths_message(limit: int) -> str:
    with tempfile.TemporaryDirectory() as component:
        with mock.patch.object(_config, "MAX_PROVIDER_DETECTION_ENTRIES", limit):
            return _guardrail_message(
                _detect_provider,
                Path(component),
                available_paths={f"f{index}.txt" for index in range(limit + 1)},
            )


def _directory_entries_message(limit: int) -> str:
    with tempfile.TemporaryDirectory() as component:
        directory = Path(component)
        for index in range(limit + 1):
            (directory / f"f{index}.txt").write_bytes(b"x")
        with mock.patch.object(_config, "MAX_PROVIDER_DETECTION_ENTRIES", limit):
            return _guardrail_message(_detect_provider, directory)


def _traversal_message(limit: int) -> str:
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        for index in range(limit + 1):
            (root / f"f{index}.txt").write_bytes(b"x")
        with mock.patch.object(
            _config, "MAX_FILESYSTEM_TRAVERSAL_ENTRIES", limit
        ):
            return _guardrail_message(discover_components, root)


#: Every production ceiling text, paired with the constant it interpolates and
#: a way to make the source produce the same sentence at an arbitrary limit.
#: `test_every_ceiling_message_is_a_template_the_source_fills_in` observes each
#: one at two limits and rebuilds the text on the right from the two
#: observations, so none of the five literals above is a transcription.
CEILING_MESSAGE_PROBES: Dict[str, Tuple[int, str, Callable[[int], str]]] = {
    "max_discovered_components": (
        MAX_DISCOVERED_COMPONENTS,
        CEILINGS["max_discovered_components"][1],
        _components_message,
    ),
    "max_discovery_manifests": (
        MAX_DISCOVERY_MANIFESTS,
        CEILINGS["max_discovery_manifests"][1],
        _manifests_message,
    ),
    "max_provider_detection_entries": (
        MAX_PROVIDER_DETECTION_ENTRIES,
        CEILINGS["max_provider_detection_entries"][1],
        _available_paths_message,
    ),
    "provider_directory_entries": (
        MAX_PROVIDER_DETECTION_ENTRIES,
        PROVIDER_DIRECTORY_ENTRIES,
        _directory_entries_message,
    ),
    "max_filesystem_traversal_entries": (
        MAX_FILESYSTEM_TRAVERSAL_ENTRIES,
        CEILINGS["max_filesystem_traversal_entries"][1],
        _traversal_message,
    ),
}


def _stage_components_ceiling(scene: Scenario, stack) -> str:
    stack.enter_context(
        _synthetic_manifest_index(MAX_DISCOVERED_COMPONENTS + 1)
    )
    return CEILINGS["max_discovered_components"][1]


def _stage_manifests_ceiling(scene: Scenario, stack) -> str:
    stack.enter_context(
        _synthetic_manifest_index(MAX_DISCOVERY_MANIFESTS + 1)
    )
    return CEILINGS["max_discovery_manifests"][1]


def _stage_provider_ceiling(scene: Scenario, stack) -> str:
    """Fifty thousand tracked siblings is not a fixture; lower the constant."""
    for index in range(PATCHED_CEILING + 1):
        scene.file(f"svc/f{index}.txt", "x\n")
    scene.file("svc/package.json", MANIFEST_BODIES["package.json"])
    scene.commit("provider entries")
    stack.enter_context(
        mock.patch.object(
            _config, "MAX_PROVIDER_DETECTION_ENTRIES", PATCHED_CEILING
        )
    )
    return (
        "Provider detection guardrail exceeded: "
        f">{PATCHED_CEILING} available paths"
    )


def _stage_traversal_ceiling(scene: Scenario, stack) -> str:
    """The crawl runs only on the fallback, which needs the index gone."""
    for index in range(PATCHED_CEILING + 1):
        scene.file(f"f{index}.txt", "x\n")
    scene.commit("traversal entries")
    stack.enter_context(
        mock.patch.object(
            _discovery, "_iter_bounded_git_paths", _git_index_unavailable
        )
    )
    stack.enter_context(
        mock.patch.object(_discovery, "_is_git_repository", return_value=False)
    )
    stack.enter_context(
        mock.patch.object(
            _config, "MAX_FILESYSTEM_TRAVERSAL_ENTRIES", PATCHED_CEILING
        )
    )
    return (
        "Component discovery guardrail exceeded: "
        f"filesystem traversal exceeds {PATCHED_CEILING} entries"
    )


#: How each of the four ceilings is reached from the command line. Each entry
#: arranges the repository and the patches and returns the exact text its
#: breach must put on stderr. Two run at the production constant; the two that
#: cannot run at `PATCHED_CEILING`, and the message-template test above is what
#: connects the lowered text back to the production one.
CLI_CEILING_STAGES: Dict[str, Callable[[Scenario, contextlib.ExitStack], str]] = {
    "max_discovered_components": _stage_components_ceiling,
    "max_discovery_manifests": _stage_manifests_ceiling,
    "max_provider_detection_entries": _stage_provider_ceiling,
    "max_filesystem_traversal_entries": _stage_traversal_ceiling,
}

#: The two discovery commands the obligation names, spelled the way a user
#: would type them. `init --discover` is the one that would otherwise write a
#: truncated config, so the test that drives this table checks the file too.
DISCOVERY_COMMANDS: Dict[str, Tuple[str, ...]] = {
    "discover": ("discover", "--format", "json"),
    "init --discover": ("init", "--discover"),
}


class DiscoveryCeilingTests(unittest.TestCase):
    """Four ceilings, each exact at its boundary and each disclosed by name."""

    def _repository(self, name: str = "ceilings") -> Scenario:
        return _ceiling_repository(name)

    def test_the_injected_name_stream_answers_like_the_real_index(self):
        """The premise: the ceilings below are reached through a faithful seam.

        The 1,000th and 50,000th manifest cannot be created on disk in a test,
        so the tracked-name source is replaced. Replacing it is only honest if
        a real repository and its own name list produce the identical component
        map, which is what this checks before the scale tests use the seam.
        """
        with self._repository("faithful") as scene:
            scene.file("api/pyproject.toml", MANIFEST_BODIES["pyproject.toml"])
            scene.file("web/package.json", MANIFEST_BODIES["package.json"])
            scene.commit("more manifests")
            real = discover_components(scene.root)
            names = scene.git("ls-files").splitlines()
            with mock.patch.object(
                _discovery, "_iter_bounded_git_paths", _tracked_names(names)
            ):
                injected = discover_components(scene.root)
        self.assertEqual(sorted(real), ["api", "web"])
        self.assertEqual(injected, real)

    def test_the_production_ceilings_are_the_numbers_the_contract_names(self):
        self.assertEqual(MAX_DISCOVERED_COMPONENTS, 1_000)
        self.assertEqual(MAX_DISCOVERY_MANIFESTS, 50_000)
        self.assertEqual(MAX_PROVIDER_DETECTION_ENTRIES, 50_000)
        self.assertEqual(MAX_FILESYSTEM_TRAVERSAL_ENTRIES, 200_000)

    def test_every_ceiling_message_is_a_template_the_source_fills_in(self):
        """The five production texts, earned rather than transcribed.

        An earlier draft closed the constants test by checking that each
        message literal contained the digits of the constant beside it - a
        comparison between two things this file had already pinned, which
        could not fail once the four assertions above had passed. What the
        table needs instead is a derivation: raise each ceiling twice at two
        different limits, require the two sentences to differ only in the
        number, and require the recovered template with the production
        constant substituted to reproduce the entry exactly. Wording drift,
        an interpolation dropped in favour of a hard-coded number, and a table
        row naming the wrong constant all fail here. The traversal and
        directory-entry texts have no other witness in this file, because
        200,000 filesystem entries and 50,000 tracked siblings are not
        fixtures anyone can build.
        """
        low, high = TEMPLATE_LIMITS
        self.assertNotEqual(low, high)
        for label, (value, production, probe) in CEILING_MESSAGE_PROBES.items():
            with self.subTest(ceiling=label):
                at_low, at_high = probe(low), probe(high)
                self.assertNotEqual(
                    at_low, at_high, "the limit has to reach the message"
                )
                template = at_low.replace(str(low), "{}")
                self.assertNotEqual(template, at_low, "nothing was substituted")
                self.assertEqual(at_high.replace(str(high), "{}"), template)
                self.assertEqual(template.format(value), production)

    def test_every_ceiling_breach_is_a_value_error_the_cli_can_catch(self):
        """`_cmd_discover` catches ValueError; GuardrailError must stay one."""
        self.assertTrue(issubclass(GuardrailError, ValueError))

    def test_the_component_ceiling_accepts_one_thousand_and_refuses_the_next(self):
        with self._repository("components") as scene:
            with _synthetic_manifest_index(MAX_DISCOVERED_COMPONENTS):
                accepted = discover_components(scene.root)
            self.assertEqual(len(accepted), MAX_DISCOVERED_COMPONENTS)
            with _synthetic_manifest_index(MAX_DISCOVERED_COMPONENTS + 1):
                with self.assertRaises(GuardrailError) as raised:
                    discover_components(scene.root)
        self.assertEqual(
            str(raised.exception), CEILINGS["max_discovered_components"][1]
        )

    def test_the_manifest_ceiling_lets_fifty_thousand_reach_the_next_ceiling(self):
        """Its accept side is visible as which guardrail fires instead.

        Fifty thousand manifests in fifty thousand directories are also fifty
        thousand components, so the accept side of this ceiling cannot show a
        successful discovery: what it shows is that the manifest limit did not
        fire, and the component limit did. One more manifest and the manifest
        limit fires first, before a single component is built.
        """
        with self._repository("manifests-ceiling") as scene:
            with _synthetic_manifest_index(MAX_DISCOVERY_MANIFESTS):
                with self.assertRaises(GuardrailError) as at_limit:
                    discover_components(scene.root)
            with _synthetic_manifest_index(MAX_DISCOVERY_MANIFESTS + 1):
                with self.assertRaises(GuardrailError) as past_limit:
                    discover_components(scene.root)
        self.assertEqual(
            str(at_limit.exception), CEILINGS["max_discovered_components"][1]
        )
        self.assertEqual(
            str(past_limit.exception), CEILINGS["max_discovery_manifests"][1]
        )

    def test_the_index_backed_provider_ceiling_is_exact_at_fifty_thousand(self):
        with tempfile.TemporaryDirectory() as component:
            directory = Path(component)
            at_limit = {
                f"f{index:06d}.txt"
                for index in range(MAX_PROVIDER_DETECTION_ENTRIES)
            }
            self.assertEqual(
                _detect_provider(directory, available_paths=at_limit),
                ("implicit", []),
            )
            past_limit = at_limit | {"one-too-many.txt"}
            self.assertEqual(len(past_limit), MAX_PROVIDER_DETECTION_ENTRIES + 1)
            with self.assertRaises(GuardrailError) as raised:
                _detect_provider(directory, available_paths=past_limit)
        self.assertEqual(
            str(raised.exception),
            CEILINGS["max_provider_detection_entries"][1],
        )

    def test_the_filesystem_provider_ceiling_refuses_one_entry_past_its_limit(self):
        """The real 50,000 cannot be created, so the boundary is shown twice.

        Two different patched limits, each with exactly that many files and
        then one more, establish both halves of the off-by-one. The accept
        side is the part that only a real fixture can show; the message itself
        is turned into the production text by
        `test_every_ceiling_message_is_a_template_the_source_fills_in`.
        """
        for limit in (3, 7):
            with self.subTest(limit=limit):
                with tempfile.TemporaryDirectory() as component:
                    directory = Path(component)
                    for index in range(limit):
                        (directory / f"f{index}.txt").write_bytes(b"x")
                    with mock.patch.object(
                        _config, "MAX_PROVIDER_DETECTION_ENTRIES", limit
                    ):
                        self.assertEqual(
                            _detect_provider(directory), ("implicit", [])
                        )
                    (directory / "one-too-many.txt").write_bytes(b"x")
                    with mock.patch.object(
                        _config, "MAX_PROVIDER_DETECTION_ENTRIES", limit
                    ):
                        with self.assertRaises(GuardrailError) as raised:
                            _detect_provider(directory)
                self.assertEqual(
                    str(raised.exception),
                    "Provider detection guardrail exceeded: "
                    f">{limit} directory entries",
                )

    def test_the_traversal_ceiling_refuses_one_entry_past_its_limit(self):
        """Same treatment: 200,000 entries is not a fixture anyone can build."""
        for limit in (4, 9):
            with self.subTest(limit=limit):
                with tempfile.TemporaryDirectory() as workspace:
                    root = Path(workspace)
                    for index in range(limit):
                        (root / f"f{index}.txt").write_bytes(b"x")
                    with mock.patch.object(
                        _config, "MAX_FILESYSTEM_TRAVERSAL_ENTRIES", limit
                    ):
                        self.assertEqual(discover_components(root), {})
                    (root / "one-too-many.txt").write_bytes(b"x")
                    with mock.patch.object(
                        _config, "MAX_FILESYSTEM_TRAVERSAL_ENTRIES", limit
                    ):
                        with self.assertRaises(GuardrailError) as raised:
                            discover_components(root)
                self.assertEqual(
                    str(raised.exception),
                    "Component discovery guardrail exceeded: "
                    f"filesystem traversal exceeds {limit} entries",
                )

    def test_discover_under_the_ceiling_prints_the_whole_payload(self):
        """The premise: a refusal is not the only thing this command can do."""
        with self._repository("under") as scene:
            with _synthetic_manifest_index(3):
                result = run_cli_in_process(
                    scene.root, "discover", "--format", "json"
                )
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["count"], 3)
        self.assertEqual(len(payload["components"]), 3)

    def test_discover_maps_a_ceiling_breach_to_exit_two_and_no_payload(self):
        with self._repository("cli-discover") as scene:
            with _synthetic_manifest_index(MAX_DISCOVERED_COMPONENTS + 1):
                result = run_cli_in_process(
                    scene.root, "discover", "--format", "json"
                )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr.strip(),
            "ERROR: component discovery failed: "
            + CEILINGS["max_discovered_components"][1],
        )
        self.assertNotIn("Traceback", result.stderr)

    def test_init_discover_under_the_ceiling_writes_a_config(self):
        """The premise for the next test: init does write when discovery works."""
        with self._repository("init-ok") as scene:
            scene.file("svc/package.json", MANIFEST_BODIES["package.json"])
            scene.commit("manifest")
            config_path = scene.root / CONFIG
            config_path.unlink()
            result = run_cli_in_process(scene.root, "init", "--discover")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(config_path.exists())
            written = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(written["components"]), ["svc"])

    def test_every_ceiling_refuses_through_both_discovery_commands(self):
        """The end-to-end clause, for all four rather than one of them.

        The obligation asks that *every* ceiling surface through `discover`
        and through `init --discover` as exit 2, the ceiling text on stderr,
        nothing on stdout and no traceback. An earlier account claimed that
        and covered only `max_discovered_components`; the other three had a
        library-level refusal and nothing more, which is the half that
        matters least - a GuardrailError that core.py failed to catch would
        become a traceback with a different exit code and this file would not
        have noticed.

        The traversal row reaches the CLI through the filesystem fallback, so
        its stderr carries the approximation warning ahead of the error; the
        error is therefore matched as the last line rather than the whole
        stream, and the two rows that stand alone keep their exact
        whole-stderr assertions below.
        """
        for label, stage in CLI_CEILING_STAGES.items():
            for spelling, command in DISCOVERY_COMMANDS.items():
                with self.subTest(ceiling=label, command=spelling):
                    with self._repository("cli-ceiling") as scene:
                        config_path = scene.root / CONFIG
                        with contextlib.ExitStack() as stack:
                            message = stage(scene, stack)
                            if command[0] == "init":
                                config_path.unlink()
                            result = run_cli_in_process(scene.root, *command)
                        wrote_config = config_path.exists()
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertEqual(
                        result.stderr.strip().splitlines()[-1],
                        "ERROR: component discovery failed: " + message,
                    )
                    if command[0] == "init":
                        self.assertFalse(wrote_config)

    def test_init_discover_maps_a_ceiling_breach_to_exit_two_and_no_config(self):
        with self._repository("cli-init") as scene:
            config_path = scene.root / CONFIG
            config_path.unlink()
            with _synthetic_manifest_index(MAX_DISCOVERED_COMPONENTS + 1):
                result = run_cli_in_process(scene.root, "init", "--discover")
            self.assertFalse(config_path.exists())
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr.strip(),
            "ERROR: component discovery failed: "
            + CEILINGS["max_discovered_components"][1],
        )
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
