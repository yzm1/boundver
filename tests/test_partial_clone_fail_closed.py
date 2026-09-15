"""A repository that promises its own contents from somewhere else.

A partial clone is a repository with holes in it. Git records a promisor
remote and fetches a skipped object the moment anything asks for one, which
turns an ordinary local read into an outbound request nobody asked for.
boundver answers with a single environment variable: every Git subprocess it
starts carries ``GIT_NO_LAZY_FETCH=1``, so a read of an absent blob comes back
as the word "missing" and the run fails closed. ``docs/reference.md`` states
that as an unconditional promise, which is why it has to be tested against a
repository actually shaped that way rather than against the dictionary the
variable is written into.

Building such a repository without a network is the awkward part. The source
has to set ``uploadpack.allowFilter`` or Git accepts ``--filter=blob:none``,
prints a warning nobody reads, and clones every blob anyway - and then every
"boundver fetched nothing" assertion is true and empty. The clone has to be
sparse as well, because a full checkout fetches the blobs the filter skipped.
What survives is a clone over ``file://`` whose promisor remote is reachable
and demonstrably willing to serve the blob boundver needs, with that blob
genuinely absent, so "still absent afterwards" says something.

Absence is the difficult kind of evidence, so every claim of it here is paired
with the same read succeeding somewhere it is allowed to. The clone is checked
for a hole at the moment it is built; each starved object is read out of a
second clone of the same shape with the guard lifted, and it arrives; and the
one spelling that refuses before it opens any object at all - ``--source
working-tree``, which stops at a missing directory - says so by leaving the
object absent even when fetching is permitted, in the same test where
``--source head`` on that same clone fetches it a line later.

Boundver also detects ``remote.<name>.promisor``, partial-clone filters, and
``extensions.partialClone`` in local configuration. Git 2.45 added the
no-lazy-fetch control; partial clones are refused on older Git while ordinary
full repositories retain the Git 2.32 baseline. On supported Git, removing the
guard in a test still demonstrates the underlying risk: the refusal becomes a
completed fetch and a lockfile byte-identical to a full clone's.

Covers OBL-GIT-SOURCE-158.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, Optional, Tuple
from unittest import mock

from boundver import _git as git_module
from boundver._lockfile import generate_lockfile
from boundver._utils import ConfigError, GuardrailError

from tests._parity import run_cli
from tests._repo_fixtures import init_git_repo

REFUSAL = 2

#: The one component under test. Its boundary is measured on a file nested
#: two directories down, so a cone-mode sparse checkout leaves that file out
#: of the working tree and its blob out of the clone.
CONFIG = {
    "project": "partial",
    "components": {
        "svc": {
            "path": "services/svc",
            "boundary": {"provider": "path-hash", "paths": ["api/*.yaml"]},
        }
    },
}

COMPONENT_FILE = "services/svc/api/v1.yaml"
CONTENT = b"openapi: 3.1.0\n"

#: The three ways a Git repository advertises that it is a partial clone,
#: matched case-insensitively against the package source. Git 2.55 writes only
#: the ``remote.<name>.*`` pair, so a detector keyed on the ``extensions`` key
#: the obligation names would miss the clone modern Git actually produces.
SHAPE_SIGNALS = ("promisor", "partialclone", "blob:none")

#: Every generate spelling that must refuse, and whether that spelling also
#: compares the working tree - only ``--source head`` does, which is where the
#: uncommitted-changes warning comes from.
GENERATE_SPELLINGS = {
    "head": (("generate", "--source", "head"), True),
    "index": (("generate", "--source", "index"), False),
    "head with --allow-partial": (
        ("generate", "--source", "head", "--allow-partial"),
        True,
    ),
}

#: Partial clones that starve a different read than the component blob: the
#: root tree and the config file. Each row names the fixture attribute holding
#: the object Git was not given, and then the whole of the stderr the refusal
#: produces - the whole of it, because the config row's diagnostic names no
#: object at all, and a substring match would let that row read as though a
#: missing-object message had been verified there. ``{head}`` is the clone's
#: HEAD commit, which is the id boundver hands to ``git ls-tree``.
STARVED_SHAPES = {
    "tree:0 without a checkout": (
        ("--filter=tree:0", "--no-checkout"),
        "root_tree",
        (
            "ERROR: Cannot capture head source: Cannot enumerate captured head "
            "tree {head}: git ls-tree failed (return code 128; stderr='fatal: "
            "not a tree object')",
        ),
    ),
    "blob:none without a checkout": (
        ("--filter=blob:none", "--no-checkout"),
        "config_blob",
        (
            "ERROR: Cannot read config from captured head source: "
            "boundary.config.json",
        ),
    ),
}

#: Every read surface reached on an absent blob, with the exit code it uses
#: and the stream that carries the diagnostic. ``status`` reports drift and
#: still exits zero; that is the current contract, pinned rather than judged.
READ_SURFACES = {
    "verify": (("verify", "--source", "head"), REFUSAL, "stdout"),
    "verify --format json": (("verify", "--format", "json"), REFUSAL, "stdout"),
    "why": (("why", "svc"), REFUSAL, "stderr"),
    "status": (("status",), 0, "stdout"),
}

HINT = (
    "Review the reported provider, source, or facet error. Use --allow-partial "
    "only when null slice facet inputs are intentional."
)

_SRC = str(Path(git_module.__file__).resolve().parents[1])


def _git(root: Path, *args: str, environment=None, check: bool = True):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=check,
        env=environment,
    )


def _config_value(root: Path, key: str) -> Optional[str]:
    """The repository-local value of *key*, or None when Git has none."""
    result = _git(root, "config", "--get", key, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def _require_installed_no_lazy_fetch_support(root: Path) -> None:
    """Skip behaviour that only Git 2.45+ can enforce.

    The product deliberately refuses partial clones on older supported Git
    versions before reading objects. Tests for the later missing-object path
    therefore apply only where ``GIT_NO_LAZY_FETCH`` exists; fixture and
    explicit old-version-gate tests remain runnable across the full matrix.
    """
    installed, display = git_module._installed_git_version(str(root.resolve()))
    if installed < git_module.MINIMUM_PARTIAL_CLONE_GIT_VERSION:
        required = ".".join(
            str(part) for part in git_module.MINIMUM_PARTIAL_CLONE_GIT_VERSION
        )
        raise unittest.SkipTest(
            f"Git {required}+ is required for no-lazy-fetch behaviour; "
            f"installed Git is {display}"
        )


def _absent_objects(root: Path) -> Tuple[str, ...]:
    """Object ids the repository knows of but does not hold.

    Run with the guard on, so asking the question cannot itself fetch the
    answer away.
    """
    environment = dict(os.environ, GIT_NO_LAZY_FETCH="1")
    result = _git(
        root,
        "rev-list",
        "--objects",
        "--all",
        "--missing=print",
        environment=environment,
    )
    return tuple(
        sorted(
            line[1:].split()[0]
            for line in result.stdout.splitlines()
            if line.startswith("?")
        )
    )


def _read_object_allowing_a_fetch(root: Path, object_id: str):
    """Ask Git for *object_id* with the guard lifted, which permits a fetch.

    Any object, not only a blob: the starved shapes below withhold a tree and
    a config blob, and the same ``cat-file --batch`` read serves both.
    """
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() != "GIT_NO_LAZY_FETCH"
    }
    return subprocess.run(
        ["git", "-C", str(root), "cat-file", "--batch"],
        input=object_id.encode("ascii") + b"\n",
        capture_output=True,
        env=environment,
    )


def _run_cli_with_ambient(root: Path, *args: str, **ambient: str):
    """`run_cli`, plus ambient variables the repository owner wants honoured.

    os.environ itself is never touched: the overrides go into a copy handed to
    the child, so nothing leaks into the rest of the suite.
    """
    environment = os.environ.copy()
    environment.update(ambient)
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = _SRC + (os.pathsep + existing if existing else "")
    return subprocess.run(
        [sys.executable, "-m", "boundver", *args],
        cwd=root,
        capture_output=True,
        text=True,
        env=environment,
    )


def _package_files_mentioning(signals) -> Dict[str, str]:
    """Which shipped module names each shape signal, if any does."""
    package = Path(git_module.__file__).resolve().parent
    found: Dict[str, str] = {}
    for path in sorted(package.rglob("*.py")):
        text = path.read_text(encoding="utf-8").lower()
        for signal in signals:
            if signal.lower() in text:
                found[path.name] = signal
    return found


@contextlib.contextmanager
def _a_git_that_ignores_the_lazy_fetch_guard():
    """Model a Git too old to know the variable by removing it at the seam.

    Every Git subprocess boundver starts takes its environment from
    ``_offline_git_environment``, so dropping the name there is exactly what a
    Git that does not recognise it would do with the value. A real old binary
    would be the truer witness; a shim on PATH is not available on Windows,
    where cmd.exe re-parses the ``|`` characters in boundver's filter-config
    regex and the run dies of something unrelated.
    """
    real = git_module._offline_git_environment

    def without_the_guard(repo_root=None, environment=None):
        result = real(repo_root, environment)
        result.pop("GIT_NO_LAZY_FETCH", None)
        return result

    with mock.patch.object(git_module, "_offline_git_environment", without_the_guard):
        yield


class PartialCloneFixture:
    """A source repository and the partial clones taken from it.

    Not a `Scenario`: the clone has to be a sibling directory of the source,
    and the source has to allow filtered fetches before any of this means
    anything.
    """

    def __init__(self, *, allow_filter: bool = True, with_lockfile: bool = False):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        init_git_repo(self.source, initial_branch="main")
        for key, value in (("core.autocrlf", "false"), ("core.eol", "lf")):
            _git(self.source, "config", key, value)
        if allow_filter:
            _git(self.source, "config", "uploadpack.allowFilter", "true")
        (self.source / "boundary.config.json").write_bytes(
            (json.dumps(CONFIG, indent=2) + "\n").encode("utf-8")
        )
        component = self.source / COMPONENT_FILE
        component.parent.mkdir(parents=True)
        component.write_bytes(CONTENT)
        _git(self.source, "add", "--all")
        _git(self.source, "commit", "-m", "fixture")
        if with_lockfile:
            seeded = run_cli(self.source, "generate", "--source", "head")
            if seeded.returncode != 0:
                raise AssertionError(f"could not seed a lockfile: {seeded.stderr}")
            _git(self.source, "add", "--all")
            _git(self.source, "commit", "-m", "lock")
        self.blob = _git(
            self.source, "rev-parse", f"HEAD:{COMPONENT_FILE}"
        ).stdout.strip()
        #: The other two objects a filter can withhold, read out of the source
        #: rather than out of a clone: asking a starved clone for them would
        #: itself be the lazy fetch these tests exist to observe.
        self.config_blob = _git(
            self.source, "rev-parse", "HEAD:boundary.config.json"
        ).stdout.strip()
        self.root_tree = _git(self.source, "rev-parse", "HEAD^{tree}").stdout.strip()
        self._clones = 0
        self._ordinaries = 0

    def clone(self, *options: str):
        """Clone over file:// so the filter is negotiated, not short-circuited."""
        self._clones += 1
        destination = self.root / f"clone{self._clones}"
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.autocrlf=false",
                "-c",
                "core.eol=lf",
                "clone",
                "--no-local",
                *options,
                self.source.as_uri(),
                str(destination),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return destination, result

    def absent_blob_clone(self) -> Path:
        """A clone whose promised component blob was never transferred.

        The hole is verified here rather than trusted downstream. A source
        that lost ``uploadpack.allowFilter``, or a Git that stopped honouring
        the filter over ``file://``, hands back a whole repository that still
        records a promisor remote, and every "nothing was fetched" assertion
        written against it would pass while measuring nothing.
        """
        destination, _ = self.clone("--filter=blob:none", "--sparse")
        missing = _absent_objects(destination)
        if missing != (self.blob,):
            raise AssertionError(
                f"the clone is not starved of {self.blob}: missing {missing!r}"
            )
        return destination

    def ordinary_repository_missing_the_same_blob(self) -> Path:
        """The same hole in a repository that is not a partial clone.

        Same content, so content addressing gives the same object id; the
        loose object is deleted from the store and the component directory
        from the working tree. What is left is what the sparse partial clone
        presents to boundver, minus every trace of the partial-clone
        configuration - no promisor remote, nothing to fetch from.
        """
        self._ordinaries += 1
        destination = self.root / f"ordinary{self._ordinaries}"
        destination.mkdir()
        init_git_repo(destination, initial_branch="main")
        for key, value in (("core.autocrlf", "false"), ("core.eol", "lf")):
            _git(destination, "config", key, value)
        (destination / "boundary.config.json").write_bytes(
            (json.dumps(CONFIG, indent=2) + "\n").encode("utf-8")
        )
        component = destination / COMPONENT_FILE
        component.parent.mkdir(parents=True)
        component.write_bytes(CONTENT)
        _git(destination, "add", "--all")
        _git(destination, "commit", "-m", "fixture")
        written = _git(
            destination, "rev-parse", f"HEAD:{COMPONENT_FILE}"
        ).stdout.strip()
        if written != self.blob:
            raise AssertionError(
                f"content addressing moved: {written} is not {self.blob}"
            )
        loose = destination / ".git" / "objects" / self.blob[:2] / self.blob[2:]
        if not loose.exists():
            raise AssertionError(f"expected a loose object at {loose}")
        loose.chmod(stat.S_IWRITE)
        loose.unlink()
        shutil.rmtree(destination / "services")
        return destination

    def close(self) -> None:
        self._directory.cleanup()


class PartialCloneFixtureTests(unittest.TestCase):
    """OBL-GIT-SOURCE-158: the fixture is a partial clone genuinely missing a blob."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = PartialCloneFixture()
        cls.addClassCleanup(cls.fixture.close)
        cls.clone = cls.fixture.absent_blob_clone()
        cls.blob = cls.fixture.blob

    def test_the_clone_records_a_reachable_promisor_remote(self):
        self.assertEqual(_config_value(self.clone, "remote.origin.promisor"), "true")
        self.assertEqual(
            _config_value(self.clone, "remote.origin.partialclonefilter"), "blob:none"
        )
        self.assertEqual(
            _config_value(self.clone, "core.repositoryformatversion"), "1"
        )
        self.assertEqual(
            _config_value(self.clone, "remote.origin.url"), self.fixture.source.as_uri()
        )

    def test_the_extensions_key_the_obligation_names_may_not_be_written_at_all(self):
        """The trap in the obligation's own premise, pinned.

        Git 2.55 announces a partial clone in the remote section and leaves
        `extensions.partialClone` unset, so a detector reading only that key
        would see an ordinary repository.
        """
        extensions = _config_value(self.clone, "extensions.partialClone")
        self.assertIn(
            extensions,
            (None, "origin"),
            "unexpected extensions.partialClone value on this Git",
        )
        if extensions is None:
            self.assertEqual(
                _config_value(self.clone, "remote.origin.promisor"),
                "true",
                "neither shape signal is present: the fixture is not a partial clone",
            )

    def test_the_component_blob_is_absent_and_its_file_is_not_checked_out(self):
        self.assertEqual(_absent_objects(self.clone), (self.blob,))
        self.assertFalse((self.clone / COMPONENT_FILE).exists())
        self.assertTrue((self.clone / "boundary.config.json").exists())

    def test_the_promisor_remote_would_serve_that_blob_if_the_guard_were_lifted(self):
        """The premise every "nothing was fetched" assertion in this file rests on.

        A fetch that could not have succeeded proves nothing about a fetch
        that did not happen, so a second clone from the same builder is asked
        for the same object with the guard removed. It arrives.
        """
        control = self.fixture.absent_blob_clone()
        self.assertEqual(_absent_objects(control), (self.blob,))
        served = _read_object_allowing_a_fetch(control, self.blob)
        self.assertEqual(served.returncode, 0, served.stderr)
        self.assertEqual(
            served.stdout,
            f"{self.blob} blob {len(CONTENT)}\n".encode("ascii") + CONTENT + b"\n",
        )
        self.assertEqual(_absent_objects(control), ())

    def test_a_server_that_refuses_the_filter_leaves_nothing_absent(self):
        """The vacuity trap: without uploadpack.allowFilter the clone is whole.

        It still exits zero and still records a promisor remote, so only the
        absent-object set distinguishes the fixture that tests something from
        the fixture that tests nothing. `absent_blob_clone` raises on such a
        clone rather than returning it, which is what keeps the rest of the
        file from quietly measuring a whole repository.
        """
        unfiltered = PartialCloneFixture(allow_filter=False)
        self.addCleanup(unfiltered.close)
        clone, result = unfiltered.clone("--filter=blob:none", "--sparse")
        self.assertIn(
            "warning: filtering not recognized by server, ignoring", result.stderr
        )
        self.assertEqual(_config_value(clone, "remote.origin.promisor"), "true")
        self.assertEqual(_absent_objects(clone), ())
        with self.assertRaises(AssertionError):
            unfiltered.absent_blob_clone()


class FailClosedOnAnAbsentBlobTests(unittest.TestCase):
    """OBL-GIT-SOURCE-158: an absent promised blob is a refusal, never a fetch."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = PartialCloneFixture()
        cls.addClassCleanup(cls.fixture.close)
        cls.clone = cls.fixture.absent_blob_clone()
        cls.blob = cls.fixture.blob
        _require_installed_no_lazy_fetch_support(cls.clone)

    def setUp(self):
        git_module._ambient_worktree_config_overrides.cache_clear()
        git_module._repository_filter_config_overrides.cache_clear()

    def tearDown(self):
        git_module._ambient_worktree_config_overrides.cache_clear()
        git_module._repository_filter_config_overrides.cache_clear()

    @property
    def absent(self) -> str:
        return f"Git blob not found for ref '{self.blob}'"

    @property
    def refusal_line(self) -> str:
        r"""The whole ERROR line, whose parts are joined by a literal \n.

        The output writer escapes the newline inside one emitted diagnostic,
        so this is one physical line carrying two backslash-n sequences.
        """
        return (
            "ERROR: Lockfile generation failed:"
            "\\n"
            f"svc: Exact digest failed: {self.absent}"
            "\\n"
            "svc: Boundary content collection failed for api/v1.yaml: "
            f"{self.absent}"
        )

    def test_every_generate_spelling_refuses_without_fetching(self):
        for label, (args, _) in GENERATE_SPELLINGS.items():
            with self.subTest(spelling=label):
                self.assertEqual(_absent_objects(self.clone), (self.blob,))
                result = run_cli(self.clone, *args)
                self.assertEqual(result.returncode, REFUSAL, result.stderr)
                self.assertIn(self.refusal_line, result.stderr.splitlines())
                self.assertIn(HINT, result.stderr)
                self.assertFalse((self.clone / "boundary.lock.json").exists())
                self.assertEqual(_absent_objects(self.clone), (self.blob,))

    def test_only_the_working_tree_comparison_warns_about_the_same_object(self):
        warning = (
            "WARNING: Could not inspect uncommitted component changes: "
            f"{self.absent}"
        )
        for label, (args, compares_the_working_tree) in GENERATE_SPELLINGS.items():
            with self.subTest(spelling=label):
                result = run_cli(self.clone, *args)
                self.assertEqual(
                    warning in result.stderr.splitlines(),
                    compares_the_working_tree,
                    f"{label} reported the uncommitted-changes warning wrongly",
                )

    def test_an_ambient_no_lazy_fetch_of_zero_does_not_weaken_the_guard(self):
        """The sanitiser overwrites the ambient value rather than merging it."""
        self.assertEqual(_absent_objects(self.clone), (self.blob,))
        result = _run_cli_with_ambient(
            self.clone, "generate", "--source", "head", GIT_NO_LAZY_FETCH="0"
        )
        self.assertEqual(result.returncode, REFUSAL, result.stderr)
        self.assertIn(self.refusal_line, result.stderr.splitlines())
        self.assertEqual(_absent_objects(self.clone), (self.blob,))

    def test_the_same_repository_with_every_blob_present_digests_that_file(self):
        """The premise: the refusal is about the absent object, not the config.

        Same source, same filter, full checkout - so the blob arrives, the
        boundary selector matches it, and both facets resolve. Without this
        the refusals above could be a broken declaration reported in the
        language of a missing object.
        """
        whole, _ = self.fixture.clone("--filter=blob:none")
        self.assertEqual(_absent_objects(whole), ())
        result = run_cli(whole, "generate", "--source", "head")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        written = json.loads(
            (whole / "boundary.lock.json").read_text(encoding="utf-8")
        )
        fingerprints = written["components"]["svc"]["fingerprints"]
        self.assertIsNotNone(fingerprints["exact"])
        self.assertIsNotNone(fingerprints["boundary"])

    def test_sparse_working_tree_fallback_never_lazily_fetches_an_object(self):
        """Sparse parity may read a local index blob but must stay offline."""
        clone = self.fixture.absent_blob_clone()
        result = run_cli(clone, "generate", "--source", "working-tree")
        self.assertEqual(result.returncode, REFUSAL)
        self.assertIn("ERROR: Config is invalid (1 issues):", result.stderr)
        self.assertIn(
            "  - Component 'svc' path not found or not a directory: services/svc",
            result.stderr,
        )
        with self.assertRaises(ConfigError):
            generate_lockfile(CONFIG, clone, source="working-tree")
        self.assertEqual(
            _absent_objects(clone),
            (self.blob,),
            "working-tree sparse fallback fetched a promised object",
        )

        # Prove the missing object was available and the refusal above came
        # from the no-lazy-fetch boundary rather than an inert fixture.
        with _a_git_that_ignores_the_lazy_fetch_guard():
            generate_lockfile(CONFIG, clone, source="working-tree")
        self.assertEqual(
            _absent_objects(clone),
            (),
            "the seam did not permit the control fetch",
        )

    def test_a_clone_starved_of_its_tree_or_its_config_also_fetches_nothing(self):
        """Each shape withholds a named object that was there for the asking.

        The control is that same object read out of a second clone of the same
        shape with the guard lifted: it arrives and it leaves that clone's
        missing set, so the object boundver declines to fetch is one the
        promisor remote would have handed over. The refusal is then pinned as
        the whole of stderr, because the config row's message names no object
        and only the exact text says so.
        """
        for label, (options, starved, lines) in STARVED_SHAPES.items():
            with self.subTest(shape=label):
                target = getattr(self.fixture, starved)
                control, _ = self.fixture.clone(*options)
                self.assertIn(target, _absent_objects(control))
                served = _read_object_allowing_a_fetch(control, target)
                self.assertEqual(served.returncode, 0, served.stderr)
                self.assertTrue(
                    served.stdout.startswith(target.encode("ascii") + b" "),
                    served.stdout[:120],
                )
                self.assertNotIn(target, _absent_objects(control))

                clone, _ = self.fixture.clone(*options)
                before = _absent_objects(clone)
                self.assertIn(target, before)
                head = _git(clone, "rev-parse", "HEAD").stdout.strip()
                result = run_cli(clone, "generate", "--source", "head")
                self.assertEqual(result.returncode, REFUSAL, result.stderr)
                self.assertEqual(
                    result.stderr.splitlines(),
                    [line.format(head=head) for line in lines],
                )
                self.assertEqual(_absent_objects(clone), before)


class UnreachablePromisorTests(unittest.TestCase):
    """OBL-GIT-SOURCE-158: the diagnostic names a missing object, not a failed fetch."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = PartialCloneFixture()
        cls.addClassCleanup(cls.fixture.close)
        cls.clone = cls.fixture.absent_blob_clone()
        cls.blob = cls.fixture.blob
        _git(
            cls.clone,
            "remote",
            "set-url",
            "origin",
            (cls.fixture.root / "no-such-source").as_uri(),
        )

    def test_a_permitted_lazy_fetch_fails_loudly_when_the_remote_is_gone(self):
        """The premise: Git has its own word for a fetch it tried and lost."""
        attempted = _read_object_allowing_a_fetch(self.clone, self.blob)
        self.assertEqual(attempted.returncode, 128)
        self.assertIn(
            f"fatal: could not fetch {self.blob} from promisor remote",
            attempted.stderr.decode("utf-8", "replace"),
        )
        self.assertEqual(_absent_objects(self.clone), (self.blob,))

    def test_boundver_reports_the_absent_object_and_never_a_failed_fetch(self):
        """Which is the whole difference between fail-closed and offline-by-luck."""
        _require_installed_no_lazy_fetch_support(self.clone)
        result = run_cli(self.clone, "generate", "--source", "head")
        self.assertEqual(result.returncode, REFUSAL)
        self.assertIn(f"Git blob not found for ref '{self.blob}'", result.stderr)
        for absent_word in ("promisor", "could not fetch"):
            with self.subTest(word=absent_word):
                self.assertNotIn(absent_word, result.stderr)
                self.assertNotIn(absent_word, result.stdout)
        self.assertEqual(_absent_objects(self.clone), (self.blob,))


class ReadSurfaceTests(unittest.TestCase):
    """OBL-GIT-SOURCE-158: every command that reads the tree fails the same way."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = PartialCloneFixture(with_lockfile=True)
        cls.addClassCleanup(cls.fixture.close)
        cls.clone = cls.fixture.absent_blob_clone()
        cls.blob = cls.fixture.blob

    @property
    def absent(self) -> str:
        return f"Git blob not found for ref '{self.blob}'"

    def test_the_lockfile_and_config_are_checked_out_but_the_blob_is_not(self):
        """The premise: these commands reach the digest, not an earlier refusal.

        A sparse checkout keeps the root-level files, so both documents verify
        needs are on disk while the file they describe is not.
        """
        self.assertTrue((self.clone / "boundary.config.json").exists())
        self.assertTrue((self.clone / "boundary.lock.json").exists())
        self.assertFalse((self.clone / COMPONENT_FILE).exists())
        self.assertEqual(_absent_objects(self.clone), (self.blob,))

    def test_every_read_surface_names_the_absent_object_without_fetching(self):
        _require_installed_no_lazy_fetch_support(self.clone)
        for label, (args, code, stream) in READ_SURFACES.items():
            with self.subTest(command=label):
                result = run_cli(self.clone, *args)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn(self.absent, getattr(result, stream))
                self.assertEqual(_absent_objects(self.clone), (self.blob,))

    def test_verify_lists_the_absent_object_as_a_current_digest_error(self):
        _require_installed_no_lazy_fetch_support(self.clone)
        result = run_cli(self.clone, "verify", "--source", "head")
        self.assertEqual(result.returncode, REFUSAL)
        self.assertIn("LOCKFILE OUT OF DATE (7 issues):", result.stdout)
        for issue in (
            f"CURRENT DIGEST ERROR svc: Exact digest failed: {self.absent}",
            "CURRENT DIGEST ERROR svc: Boundary content collection failed for "
            f"api/v1.yaml: {self.absent}",
            "METADATA MISMATCH svc.boundary_status: lockfile='ok' current='error'",
        ):
            with self.subTest(issue=issue[:40]):
                self.assertIn(issue, result.stdout)

    def test_the_json_view_says_not_ok_and_carries_the_same_two_issues(self):
        _require_installed_no_lazy_fetch_support(self.clone)
        result = run_cli(self.clone, "verify", "--format", "json")
        self.assertEqual(result.returncode, REFUSAL)
        payload = json.loads(result.stdout)
        self.assertIs(payload["ok"], False)
        self.assertIn(
            f"CURRENT DIGEST ERROR svc: Exact digest failed: {self.absent}",
            payload["issues"],
        )
        self.assertIn(
            "CURRENT DIGEST ERROR svc: Boundary content collection failed for "
            f"api/v1.yaml: {self.absent}",
            payload["issues"],
        )
        self.assertEqual(_absent_objects(self.clone), (self.blob,))

    def test_why_refuses_before_it_can_compare_anything(self):
        _require_installed_no_lazy_fetch_support(self.clone)
        result = run_cli(self.clone, "why", "svc")
        self.assertEqual(result.returncode, REFUSAL)
        self.assertIn(
            "ERROR: could not compute current fingerprints: Lockfile generation "
            f"failed:\\nsvc: Exact digest failed: {self.absent}",
            result.stderr,
        )

    def test_status_reports_the_drift_and_still_exits_zero(self):
        """Pinned as it stands: the exit code does not follow the diagnosis."""
        _require_installed_no_lazy_fetch_support(self.clone)
        result = run_cli(self.clone, "status")
        self.assertEqual(result.returncode, 0)
        self.assertIn("DRIFT DETECTED (7 issues):", result.stdout)
        self.assertIn(self.absent, result.stdout)


class WorktreeConfigShapeTests(unittest.TestCase):
    """Partial-clone signals in every effective repository scope are visible."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        init_git_repo(self.root, initial_branch="main")

    def test_the_detector_reads_a_worktree_scoped_promisor(self):
        _git(self.root, "config", "extensions.worktreeConfig", "true")
        _git(self.root, "config", "--worktree", "remote.origin.promisor", "true")

        signals = git_module._partial_clone_signals(str(self.root.resolve()))

        self.assertIn("remote.origin.promisor", signals)

    def test_old_git_is_refused_for_a_worktree_scoped_promisor(self):
        _git(self.root, "config", "extensions.worktreeConfig", "true")
        _git(self.root, "config", "--worktree", "remote.origin.promisor", "true")

        with self.assertRaisesRegex(GuardrailError, "Git 2.45.0 or newer"):
            git_module._require_safe_partial_clone_support(
                str(self.root.resolve()),
                (2, 44, 9),
                "2.44.9",
            )

    def test_the_detector_reads_repository_includes_used_by_git(self):
        included = self.root / "partial-clone.inc"
        included.write_text(
            '[remote "included"]\n\tpromisor = true\n',
            encoding="utf-8",
        )
        _git(self.root, "config", "include.path", str(included))

        signals = git_module._partial_clone_signals(str(self.root.resolve()))

        self.assertIn("remote.included.promisor", signals)

    def test_the_effective_query_still_suppresses_ambient_global_config(self):
        global_config = self.root / "ambient-global.config"
        global_config.write_text(
            '[remote "ambient"]\n\tpromisor = true\n',
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ,
            {"GIT_CONFIG_GLOBAL": str(global_config)},
            clear=False,
        ):
            signals = git_module._partial_clone_signals(str(self.root.resolve()))

        self.assertNotIn("remote.ambient.promisor", signals)


class ShapeDetectionTests(unittest.TestCase):
    """OBL-GIT-SOURCE-158: partial-clone shape and version-aware refusal."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = PartialCloneFixture()
        cls.addClassCleanup(cls.fixture.close)
        cls.blob = cls.fixture.blob
        #: Taken here, outside any test, so a degraded fixture errors the class
        #: instead of making an absence assertion pass vacuously.
        cls.starved = cls.fixture.absent_blob_clone()

    def setUp(self):
        git_module._ambient_worktree_config_overrides.cache_clear()
        git_module._repository_filter_config_overrides.cache_clear()

    def tearDown(self):
        git_module._ambient_worktree_config_overrides.cache_clear()
        git_module._repository_filter_config_overrides.cache_clear()

    def test_a_partial_clone_holding_every_blob_it_needs_is_not_refused(self):
        """The false positive a shape detector must not introduce.

        This repository is a partial clone with a promisor remote, and every
        read it serves is local. Refusing it would break every legitimate
        partial-clone user, so exit zero here is the requirement, not the bug.
        """
        whole, _ = self.fixture.clone("--filter=blob:none")
        _require_installed_no_lazy_fetch_support(whole)
        self.assertEqual(_config_value(whole, "remote.origin.promisor"), "true")
        self.assertEqual(_absent_objects(whole), ())
        result = run_cli(whole, "generate", "--source", "head")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertTrue((whole / "boundary.lock.json").exists())

    def test_the_package_reads_the_partial_clone_shape(self):
        """The shipped detector names at least one partial-clone signal."""
        self.assertNotEqual(_package_files_mentioning(SHAPE_SIGNALS), {})

    def test_the_detector_reads_the_modern_promisor_keys(self):
        signals = git_module._partial_clone_signals(str(self.starved.resolve()))
        self.assertIn("remote.origin.promisor", signals)
        self.assertIn("remote.origin.partialclonefilter", signals)

    def test_the_answer_is_the_same_one_an_ordinary_broken_repository_gets(self):
        """The behavioural pin: boundver cannot tell the two repositories apart.

        They differ in exactly the respect a detector would read - one records
        `remote.origin.promisor`, the other has no remote at all - and in
        nothing else that reaches the command: same object id withheld, same
        missing directory, same config on disk. boundver answers both with the
        same exit code and the same bytes on both streams, which is what
        "nothing detects the shape" means where a user can see it. Any
        detector that surfaced anything, however it was worded, parts them.
        """
        promisor = self.fixture.absent_blob_clone()
        _require_installed_no_lazy_fetch_support(promisor)
        ordinary = self.fixture.ordinary_repository_missing_the_same_blob()
        self.assertEqual(_config_value(promisor, "remote.origin.promisor"), "true")
        self.assertIsNone(_config_value(ordinary, "remote.origin.promisor"))
        self.assertEqual(_absent_objects(ordinary), (self.blob,))
        against_the_clone = run_cli(promisor, "generate", "--source", "head")
        against_the_repository = run_cli(ordinary, "generate", "--source", "head")
        self.assertEqual(against_the_clone.returncode, REFUSAL)
        self.assertIn(self.blob, against_the_clone.stderr)
        self.assertEqual(
            (
                against_the_repository.returncode,
                against_the_repository.stdout,
                against_the_repository.stderr,
            ),
            (
                against_the_clone.returncode,
                against_the_clone.stdout,
                against_the_clone.stderr,
            ),
        )
        self.assertEqual(_absent_objects(promisor), (self.blob,))
        self.assertEqual(_absent_objects(ordinary), (self.blob,))

    def test_the_offline_allowlist_reaches_version_but_never_fetch(self):
        self.assertIn("version", git_module._OFFLINE_GIT_SUBCOMMANDS)
        self.assertNotIn("fetch", git_module._OFFLINE_GIT_SUBCOMMANDS)

    def test_the_modelled_old_git_differs_by_exactly_the_one_variable(self):
        """The contrast the two tests below claim, measured rather than asserted.

        `_offline_git_environment` has two branches, one building the
        environment from scratch and one sanitising a mapping it is handed,
        and boundver's Git subprocesses use both. The patch has to be the
        removal of GIT_NO_LAZY_FETCH from each of them and the movement of
        nothing else, or "one variable is the difference" is a claim about a
        seam nobody looked at.
        """
        supplied = {"PATH": os.environ.get("PATH", ""), "GIT_NO_LAZY_FETCH": "0"}
        real_default = git_module._offline_git_environment(self.starved)
        real_supplied = git_module._offline_git_environment(self.starved, supplied)
        with _a_git_that_ignores_the_lazy_fetch_guard():
            modelled_default = git_module._offline_git_environment(self.starved)
            modelled_supplied = git_module._offline_git_environment(
                self.starved, supplied
            )
        for label, real, modelled in (
            ("no environment supplied", real_default, modelled_default),
            ("a supplied environment", real_supplied, modelled_supplied),
        ):
            with self.subTest(branch=label):
                self.assertEqual(real.get("GIT_NO_LAZY_FETCH"), "1")
                self.assertNotIn("GIT_NO_LAZY_FETCH", modelled)
                self.assertEqual(
                    {
                        name: value
                        for name, value in real.items()
                        if name != "GIT_NO_LAZY_FETCH"
                    },
                    modelled,
                )

    def test_the_real_git_refuses_the_same_call_and_leaves_the_object_absent(self):
        """The control for the two tests below: one variable is the difference."""
        _require_installed_no_lazy_fetch_support(self.starved)
        self.assertEqual(_absent_objects(self.starved), (self.blob,))
        with self.assertRaises(ConfigError) as raised:
            generate_lockfile(CONFIG, self.starved, source="head")
        self.assertIn(
            f"Git blob not found for ref '{self.blob}'", str(raised.exception)
        )
        self.assertEqual(_absent_objects(self.starved), (self.blob,))

    def test_removing_the_guard_alone_turns_the_refusal_into_a_completed_fetch(self):
        """Pin what a Git that ignores the variable produces today.

        The digest is the one a full clone produces, so the run that fetched
        is indistinguishable from the run that did not - which is why the
        refusal cannot be left resting on the variable alone.
        """
        _require_installed_no_lazy_fetch_support(self.starved)
        whole, _ = self.fixture.clone("--filter=blob:none")
        expected = generate_lockfile(CONFIG, whole, source="head")
        clone = self.fixture.absent_blob_clone()
        self.assertEqual(_absent_objects(clone), (self.blob,))
        with _a_git_that_ignores_the_lazy_fetch_guard():
            produced = generate_lockfile(CONFIG, clone, source="head")
        self.assertEqual(
            produced["components"]["svc"]["fingerprints"]["exact"],
            expected["components"]["svc"]["fingerprints"]["exact"],
        )
        self.assertEqual(
            produced["components"]["svc"]["fingerprints"]["boundary"],
            expected["components"]["svc"]["fingerprints"]["boundary"],
        )
        self.assertEqual(_absent_objects(clone), ())

    def test_a_git_too_old_for_the_guard_is_refused_before_object_reads(self):
        clone = self.fixture.absent_blob_clone()
        self.assertEqual(_absent_objects(clone), (self.blob,))
        with self.assertRaises(GuardrailError) as raised:
            git_module._require_safe_partial_clone_support(
                str(clone.resolve()), (2, 44, 9), "2.44.9"
            )
        self.assertIn("Git 2.44.9", str(raised.exception))
        self.assertIn("Git 2.45.0 or newer", str(raised.exception))
        self.assertEqual(_absent_objects(clone), (self.blob,))


if __name__ == "__main__":
    unittest.main()
