"""Six places where boundver's answer has to be exactly the one it claims.

The first obligation turns on a distinction no existing test can draw, because
every test that reaches the code path breaks two things at once.
``_list_files_for_source`` refuses to downgrade a working repository to a
filesystem walk: when ``ls-files --cached`` fails it re-probes with
``rev-parse --git-dir`` and re-raises unless that probe fails too. Every test
that reaches the fallback today runs in a directory that is not a repository
at all — ``test_hashing.py`` and ``test_gitignore_fallback.py`` use a bare
temporary directory, and ``test_embeddable_api_conformance.py`` moves ``.git``
out from under the call — so the listing and the probe fail together, and
deleting the re-probe would leave all of them green while boundver answered a
real Git outage with an approximate fingerprint. The fix is to fail one call
should fail: patching ``_iter_git_nul_records`` breaks the record stream
``ls-files`` rides on and leaves ``_git_run`` real, so the probe runs against
the genuine repository and succeeds. What that buys is visible in the numbers
— the fallback is not a slightly different answer but a wrong one, and the
tests below watch it return ``svc/untracked.py`` beside the tracked file Git
would have listed alone. The stderr warning itself is already pinned elsewhere
for the non-repository case; what is new here is its absence on the re-raise
path, and a recorder that shows the probe is reached for ``working-tree`` and
never for ``index``.

The non-UTF-8 filename obligation is written from a POSIX standpoint and
cannot be tested that way on this host, which took some finding out. Windows
sets the filesystem codec to UTF-8 with ``surrogatepass`` rather than
``surrogateescape``, so ``os.fsdecode(b"data/\\xff.bin")`` raises instead of
escaping; and Git for Windows converts an unpaired surrogate in a command-line
argument to U+FFFD, so ``git add`` cannot even stage such a name. Two
manoeuvres get the obligation tested anyway. ``git mktree -z`` reads its
entries from stdin as raw bytes, so a tree carrying any byte sequence at all
can be built and committed without that sequence passing through argument
encoding. And the Windows-representable analogue of an undecodable name is an
unpaired surrogate: ``b"da\\xed\\xb3\\xbf.bin"`` is not valid UTF-8, it decodes
to ``"da\\udcff.bin"`` under both hosts' codecs, ``os.fsencode`` maps it back
byte for byte, and Python — unlike Git — can create and ``lstat`` it. That
fixture drives the whole pipeline, and the interesting observation is that
``git status`` reports the file as deleted plus an unrelated untracked
U+FFFD-named one while ``_working_tree_name_status`` correctly reports
nothing: boundver reads the path with Python and is right where Git's own
porcelain is wrong. The premise for that empty list is the same fixture with
the file unlinked, which reports ``('D', 'svc/da\\udcff.bin')`` with the
surrogate intact.

The two compat-digest obligations ask opposite questions about the same
unframed concatenation and get opposite answers, so both are recorded rather
than merged. The pair OBL-HASHING-026 names — ``svc`` with identity ``1``
against ``svc@compat:1`` with an empty identity — does not collide, because
the second spells ``svc@compat:1@compat:`` and the trailing delimiter is still
there. The pair OBL-HASHING-095 names does collide, exactly as it claims, and
the test asserts that equality rather than hiding it. What makes the collision
unreachable is neither of the remedies that obligation proposes: component
names may contain ``@compat:`` today and ``validate_config`` accepts them, but
the identity half is never attacker-controlled, because it is whatever
``parse_semver`` returns for the major or major.minor core and that is digits
and dots. A property over arbitrary version strings pins that barrier, since
it is the only thing standing between the hazard and a real shared
fingerprint.

Covers OBL-GIT-SOURCE-041, OBL-GIT-SOURCE-101, OBL-GLOBS-030, OBL-GLOBS-053,
OBL-HASHING-026 and OBL-HASHING-095.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from boundver import _git as git
from boundver._config import validate_config
from boundver._git import (
    _iter_bounded_git_paths,
    _list_files_for_source,
    _parse_ls_tree_record,
    _repository_filter_config_overrides,
    _working_tree_name_status,
    changed_paths_since_ref,
    list_head_files,
)
from boundver._hashing import (
    HASH_DOMAIN_BOUNDARY,
    _hash_framed_entries,
    sha256_hex,
)
from boundver._utils import (
    ConfigError,
    GuardrailError,
    _match_path_glob,
    _PathGlobOperation,
)
from boundver.versions import parse_semver

from tests._scenarios import SOURCE_MODES, Scenario

PROFILE = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much],
)


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-041: a Git failure inside a real repository is not a fallback
# ---------------------------------------------------------------------------


def _failing_records(*args: Any, **kwargs: Any):
    """Fail the record stream that ``ls-files --cached`` rides on, only.

    ``_list_files_for_source`` reaches ``ls-files`` through
    ``_iter_bounded_git_paths`` and reaches its ``rev-parse --git-dir``
    re-probe through ``_git_run``. Breaking the first and leaving the second
    real is what separates "fallback when the probe also fails" from "fallback
    whenever ls-files fails"; every existing fallback test runs outside a
    repository, where both fail together and the distinction disappears.
    """
    raise subprocess.CalledProcessError(128, ["git", "ls-files"], b"", b"boom")
    yield  # pragma: no cover - unreachable, but makes this a generator


def _failing_run(*args: Any, **kwargs: Any):
    raise subprocess.CalledProcessError(128, ["git", "rev-parse"], "", "boom")


class _RecordingGitRun:
    """A real ``_git_run`` that remembers the argument lists it was given."""

    def __init__(self) -> None:
        self.inner = git._git_run
        self.calls: List[Tuple[str, ...]] = []

    def __call__(self, repo_root: Path, args: List[str], **kwargs: Any):
        self.calls.append(tuple(args))
        return self.inner(repo_root, args, **kwargs)


#: The exact warning the filesystem-enumeration path prints. Copied from the
#: observed stderr, not from the source, so a reworded message is a failure
#: rather than a silently updated constant.
FALLBACK_WARNING = (
    "WARNING: git file listing failed; falling back to filesystem "
    "enumeration. Fingerprints may differ from git-based computation.\n"
)

#: The two source modes that share the tracked-file set. ``head`` takes a
#: different branch entirely and is not part of this obligation.
TRACKED_SOURCES = ("index", "working-tree")


class TrackedFileEnumerationFailureTests(unittest.TestCase):
    """OBL-GIT-SOURCE-041."""

    def setUp(self) -> None:
        _repository_filter_config_overrides.cache_clear()

    def tearDown(self) -> None:
        _repository_filter_config_overrides.cache_clear()

    def _repository(self) -> Scenario:
        scene = Scenario()
        scene.component("svc", path="svc", provider="leaf")
        scene.file("svc/tracked.py", "x\n")
        scene.file("svc/nested/deep.py", "y\n")
        scene.commit()
        return scene

    # ---- premises ------------------------------------------------------

    def test_the_git_dir_probe_really_succeeds_inside_the_fixture(self):
        """The premise for every 'it re-raised' assertion below."""
        with self._repository() as scene:
            result = git._git_run(scene.root, ["rev-parse", "--git-dir"])
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), ".git")

    def test_the_fallback_runs_and_warns_when_the_probe_also_fails(self):
        """The premise: this mechanism does fire, and stderr does capture it.

        Without this, 'no warning was written' would be satisfied by a code
        path that was never entered.
        """
        with self._repository() as scene:
            captured = io.StringIO()
            with mock.patch.object(git, "_iter_git_nul_records", _failing_records), \
                 mock.patch.object(git, "_git_run", _failing_run), \
                 redirect_stderr(captured):
                files = _list_files_for_source(scene.root, "svc", "working-tree")
            self.assertEqual(files, ["svc/nested/deep.py", "svc/tracked.py"])
            self.assertEqual(captured.getvalue(), FALLBACK_WARNING)

    def test_the_fallback_answer_disagrees_with_the_git_answer(self):
        """Why the re-probe matters: the fallback is wrong, not merely slower."""
        with self._repository() as scene:
            scene.file("svc/untracked.py", "z\n")
            truth = _list_files_for_source(scene.root, "svc", "working-tree")
            self.assertEqual(truth, ["svc/nested/deep.py", "svc/tracked.py"])
            captured = io.StringIO()
            with mock.patch.object(git, "_iter_git_nul_records", _failing_records), \
                 mock.patch.object(git, "_git_run", _failing_run), \
                 redirect_stderr(captured):
                approximate = _list_files_for_source(
                    scene.root, "svc", "working-tree"
                )
            self.assertEqual(
                approximate,
                ["svc/nested/deep.py", "svc/tracked.py", "svc/untracked.py"],
            )
            self.assertNotEqual(approximate, truth)

    # ---- the obligation ------------------------------------------------

    def test_a_failed_ls_files_inside_a_real_repository_propagates(self):
        for source in TRACKED_SOURCES:
            with self.subTest(source=source):
                with self._repository() as scene:
                    with mock.patch.object(
                        git, "_iter_git_nul_records", _failing_records
                    ):
                        with self.assertRaises(
                            subprocess.CalledProcessError
                        ) as raised:
                            _list_files_for_source(scene.root, "svc", source)
                    # The re-raise carries the ls-files failure, not a fresh
                    # rev-parse one, so a rewritten `raise exc` would show up.
                    self.assertEqual(raised.exception.cmd, ["git", "ls-files"])
                    self.assertEqual(raised.exception.returncode, 128)

    def test_no_fallback_warning_is_written_when_the_probe_succeeds(self):
        for source in TRACKED_SOURCES:
            with self.subTest(source=source):
                with self._repository() as scene:
                    captured = io.StringIO()
                    with mock.patch.object(
                        git, "_iter_git_nul_records", _failing_records
                    ), redirect_stderr(captured):
                        with self.assertRaises(subprocess.CalledProcessError):
                            _list_files_for_source(scene.root, "svc", source)
                    self.assertEqual(captured.getvalue(), "")

    def test_the_index_source_re_raises_without_probing_the_git_dir(self):
        """The index branch raises before the probe; working-tree probes once.

        Two assertions in one fixture so the second is the premise for the
        first: the recorder is demonstrably able to see a ``rev-parse
        --git-dir`` call, and for ``index`` it sees none.
        """
        observed: Dict[str, List[Tuple[str, ...]]] = {}
        for source in TRACKED_SOURCES:
            with self._repository() as scene:
                recorder = _RecordingGitRun()
                with mock.patch.object(
                    git, "_iter_git_nul_records", _failing_records
                ), mock.patch.object(git, "_git_run", recorder):
                    with self.assertRaises(subprocess.CalledProcessError):
                        _list_files_for_source(scene.root, "svc", source)
                observed[source] = [
                    call for call in recorder.calls
                    if call == ("rev-parse", "--git-dir")
                ]
        self.assertEqual(observed["working-tree"], [("rev-parse", "--git-dir")])
        self.assertEqual(observed["index"], [])


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-101: a tracked name the filesystem codec cannot spell
# ---------------------------------------------------------------------------

#: An unpaired low surrogate, spelled in the WTF-8 form Git stores. It is not
#: valid UTF-8, ``os.fsdecode`` maps it to ``"\udcff"`` under both a POSIX
#: ``surrogateescape`` codec and Windows' ``surrogatepass`` one, and Python can
#: create and ``lstat`` the resulting name on either host. Git for Windows
#: cannot: it rewrites the surrogate to U+FFFD when the name arrives as a
#: command-line argument, which is why the fixture below builds trees with
#: ``mktree -z`` and materializes the file with ``pathlib``.
ODD_BYTES = b"da\xed\xb3\xbf.bin"
ODD_NAME = os.fsdecode(ODD_BYTES)
ODD_PATH = f"svc/{ODD_NAME}"


def _host_decodes(raw: bytes) -> bool:
    try:
        os.fsdecode(raw)
    except UnicodeDecodeError:
        return False
    return True


#: True where the filesystem codec is total, as POSIX ``surrogateescape``
#: makes it. False on Windows, whose ``surrogatepass`` handler passes
#: surrogates through but still rejects a bare ``0xff``.
HOST_DECODES_ANY_BYTE = _host_decodes(b"\xff")

#: Byte fragments a path segment may be built from. The last three are
#: undecodable on a ``surrogatepass`` host and are filtered out there; the
#: pinning tests below record what happens to them instead.
NAME_ATOMS = (
    b"a",
    b"9",
    b".bin",
    b"\xc3\xa9",
    b"\xf0\x9f\x92\xa9",
    b"\xed\xb3\xbf",
    b"\xed\xa0\x80",
    b"\xff",
    b"\x80",
    b"\xc3",
)

_byte_names = (
    st.lists(st.sampled_from(NAME_ATOMS), min_size=1, max_size=4)
    .map(b"".join)
    .filter(_host_decodes)
)


def _git_raw(scene: Scenario, args: List[str], stdin: Optional[bytes] = None) -> bytes:
    """Run git with byte-exact stdin and stdout, bypassing text mode."""
    return subprocess.run(
        ["git", *args],
        cwd=scene.root,
        input=stdin,
        capture_output=True,
        check=True,
    ).stdout


def _commit_tree_carrying(scene: Scenario, raw_name: bytes) -> str:
    """Commit a tree holding ``svc/<raw_name>`` and point HEAD and index at it.

    ``mktree -z`` reads entries from stdin, so the path is never encoded as a
    process argument and survives verbatim into the object store. That is the
    only route to a tracked name Git for Windows would otherwise rewrite.
    """
    blob = _git_raw(scene, ["hash-object", "-w", "--stdin"], b"payload\n")
    plain = _git_raw(scene, ["rev-parse", "HEAD:svc/plain.txt"])
    config = _git_raw(scene, ["rev-parse", "HEAD:boundary.config.json"])
    inner = _git_raw(
        scene,
        ["mktree", "-z"],
        b"100644 blob " + plain.strip() + b"\tplain.txt\x00"
        b"100644 blob " + blob.strip() + b"\t" + raw_name + b"\x00",
    )
    top = _git_raw(
        scene,
        ["mktree", "-z"],
        b"100644 blob " + config.strip() + b"\tboundary.config.json\x00"
        b"040000 tree " + inner.strip() + b"\tsvc\x00",
    )
    commit = _git_raw(
        scene,
        ["commit-tree", top.decode().strip(), "-p", scene.head(), "-m", "odd"],
    ).decode().strip()
    _git_raw(scene, ["update-ref", "HEAD", commit])
    _git_raw(scene, ["read-tree", commit])
    return commit


class SurrogateEscapedPathPipelineTests(unittest.TestCase):
    """OBL-GIT-SOURCE-101."""

    def setUp(self) -> None:
        _repository_filter_config_overrides.cache_clear()

    def tearDown(self) -> None:
        _repository_filter_config_overrides.cache_clear()

    def _repository(self) -> Scenario:
        scene = Scenario()
        scene.component("svc", path="svc", provider="path-hash", boundary=["*.bin"])
        scene.file("svc/plain.txt", "x\n")
        scene.commit()
        return scene

    def _odd_repository(self) -> Tuple[Scenario, str]:
        scene = self._repository()
        try:
            parent = scene.head()
            commit = _commit_tree_carrying(scene, ODD_BYTES)
            (scene.root / "svc" / ODD_NAME).write_bytes(b"payload\n")
        except BaseException:
            scene.close()
            raise
        self.assertNotEqual(commit, parent)
        return scene, parent

    # ---- premises ------------------------------------------------------

    def test_the_fixture_name_is_not_valid_utf8_and_still_round_trips(self):
        """The premise for calling this a surrogate-escaped name at all."""
        with self.assertRaises(UnicodeDecodeError):
            ODD_BYTES.decode("utf-8")
        self.assertEqual(ODD_NAME, "da\udcff.bin")
        self.assertEqual(os.fsencode(ODD_NAME), ODD_BYTES)

    def test_the_round_trip_oracle_rejects_a_wrong_decoding(self):
        """The premise: byte identity is a discriminating check, not a tautology."""
        mistaken = b"\xc3\xa9".decode("latin-1")
        self.assertNotEqual(os.fsencode(mistaken), b"\xc3\xa9")

    def test_a_removed_tracked_file_is_reported_as_deleted(self):
        """The premise for the empty name-status below.

        Both spellings are removed so the assertion cannot pass because the
        comparison only ever notices ASCII names.
        """
        scene, _parent = self._odd_repository()
        with scene:
            (scene.root / "svc" / ODD_NAME).unlink()
            (scene.root / "svc" / "plain.txt").unlink()
            self.assertEqual(
                _working_tree_name_status(scene.root, "HEAD"),
                [("D", ODD_PATH), ("D", "svc/plain.txt")],
            )

    # ---- the obligation ------------------------------------------------

    @PROFILE
    @given(raw=_byte_names)
    def test_a_bounded_git_path_encodes_back_to_the_bytes_git_sent(self, raw):
        """os.fsdecode in _iter_bounded_git_paths must lose no byte."""
        record = b"svc/" + raw

        def one_record(repo_root, args, **kwargs):
            yield record

        with mock.patch.object(git, "_iter_git_nul_records", one_record):
            yielded = list(_iter_bounded_git_paths(Path("."), ["ls-files"]))
        self.assertEqual(len(yielded), 1)
        self.assertEqual(os.fsencode(yielded[0]), record)

    @PROFILE
    @given(raw=_byte_names)
    def test_the_ls_tree_record_parser_encodes_back_to_the_original_bytes(self, raw):
        record = b"100644 blob " + b"0" * 40 + b"\tsvc/" + raw
        entry, path_bytes = _parse_ls_tree_record(record)
        self.assertEqual(os.fsencode(entry.path), b"svc/" + raw)
        self.assertEqual(path_bytes, len(b"svc/" + raw))

    def test_a_surrogate_named_tracked_file_is_not_reported_as_deleted(self):
        scene, _parent = self._odd_repository()
        with scene:
            self.assertEqual(_working_tree_name_status(scene.root, "HEAD"), [])

    def test_git_porcelain_is_the_one_that_gets_this_wrong(self):
        """Not an obligation - the reason dirty_component_paths is not used here.

        Git for Windows reads the directory through a lossy wide-to-UTF-8
        conversion, so it reports the tracked name deleted and an unrelated
        U+FFFD name untracked. boundver's own comparison, asserted above, does
        not. Recording the divergence keeps a later reader from "fixing"
        boundver to agree with the porcelain.
        """
        if HOST_DECODES_ANY_BYTE:
            self.skipTest("the lossy wide-character conversion is Windows-only")
        scene, _parent = self._odd_repository()
        with scene:
            self.assertEqual(
                scene.git("status", "--porcelain"),
                'D "svc/da\\355\\263\\277.bin"\n?? "svc/da\\357\\277\\275.bin"',
            )

    def test_every_source_mode_lists_and_hashes_the_surrogate_named_file(self):
        scene, _parent = self._odd_repository()
        with scene:
            self.assertEqual(
                list_head_files(scene.root, "svc"), [ODD_PATH, "svc/plain.txt"]
            )
            for source in SOURCE_MODES:
                with self.subTest(source=source):
                    # working-tree additionally lstats every tracked path to
                    # drop the ones deleted on disk, so this is the enumeration
                    # that would silently lose the file if the name did not
                    # survive os.fsdecode.
                    self.assertEqual(
                        _list_files_for_source(scene.root, "svc", source),
                        [ODD_PATH, "svc/plain.txt"],
                    )
            digests = {
                source: scene.generate(source=source)["components"]["svc"][
                    "fingerprints"
                ]
                for source in SOURCE_MODES
            }
            self.assertEqual(
                list({tuple(sorted(value.items())) for value in digests.values()}),
                [tuple(sorted(digests["head"].items()))],
            )
            self.assertIsNotNone(digests["head"]["boundary"])

    def test_changed_paths_since_ref_names_the_surrogate_path(self):
        scene, parent = self._odd_repository()
        with scene:
            for source in ("head", "index", "working-tree"):
                with self.subTest(source=source):
                    self.assertEqual(
                        changed_paths_since_ref(scene.root, parent, source),
                        [ODD_PATH],
                    )

    # ---- what a strictly undecodable name does on this host -------------

    @unittest.skipUnless(
        HOST_DECODES_ANY_BYTE,
        "this host's filesystem codec rejects a bare 0xff",
    )
    def test_a_strictly_undecodable_head_path_survives_where_the_codec_is_total(self):
        """The obligation's own example, on a host whose codec can hold it."""
        with self._repository() as scene:
            _commit_tree_carrying(scene, b"da\xff.bin")
            expected = os.fsdecode(b"svc/da\xff.bin")
            self.assertEqual(
                list_head_files(scene.root, "svc"),
                sorted([expected, "svc/plain.txt"]),
            )
            self.assertIsNotNone(
                scene.generate(source="head")["components"]["svc"]["fingerprints"][
                    "boundary"
                ]
            )

    @unittest.skipIf(
        HOST_DECODES_ANY_BYTE,
        "only a surrogatepass host refuses a bare 0xff",
    )
    def test_a_strictly_undecodable_head_path_fails_closed_on_this_host(self):
        """Known divergence, host-conditional: no surrogateescape on Windows.

        ``_decode_git_text`` asks for ``surrogateescape`` explicitly, but
        ``_iter_bounded_git_paths`` uses ``os.fsdecode``, which follows
        ``sys.getfilesystemencodeerrors()`` — ``surrogatepass`` here. A tree
        carrying ``0xff`` in a path therefore raises rather than surviving the
        pipeline. It fails closed rather than mangling, and Git for Windows
        cannot check such a tree out in the first place, so this is recorded
        rather than filed as a defect.
        """
        self.assertEqual(sys.getfilesystemencodeerrors(), "surrogatepass")
        with self._repository() as scene:
            _commit_tree_carrying(scene, b"da\xff.bin")
            with self.assertRaises(UnicodeDecodeError):
                list_head_files(scene.root, "svc")
            with self.assertRaises(ConfigError) as raised:
                scene.generate(source="head")
            self.assertIn(
                "'utf-8' codec can't decode byte 0xff", str(raised.exception)
            )


# ---------------------------------------------------------------------------
# OBL-GLOBS-030: segment containment and whole-segment ``**``
# ---------------------------------------------------------------------------

#: Candidate, pattern and the answer ``_match_path_glob`` must give. The first
#: block is the containment rule; the second is ``**`` as a whole segment; the
#: third is the half no test reaches today - ``**`` glued to anything else is
#: an ordinary wildcard and must stay inside one segment.
SEGMENT_CASES: Dict[str, Tuple[str, str, bool]] = {
    "star stops at the separator": ("src/vendor/thing.py", "src/*.py", False),
    "star matches within a segment": ("src/thing.py", "src/*.py", True),
    "star does not span one level": ("src/vendor/x.py", "src/*.py", False),
    "question mark never spans": ("a/b", "a?b", False),
    "question mark within a segment": ("axb", "a?b", True),
    "class never spans": ("a/b", "a[0-9]b", False),
    "class within a segment": ("a1b", "a[0-9]b", True),
    "recursive spans zero segments": ("a/b", "a/**/b", True),
    "recursive spans one segment": ("a/x/b", "a/**/b", True),
    "recursive spans two segments": ("a/x/y/b", "a/**/b", True),
    "recursive as a trailing segment": ("a/b/c", "a/**", True),
    "recursive consumes nothing at the end": ("a", "a/**", True),
    "recursive alone takes everything": ("a/b/c", "**", True),
    "repeated recursive collapses": ("a/b/c", "a/**/**/c", True),
    "infix pair matches inside a segment": ("axxb", "a**b", True),
    "infix pair matches an empty run": ("ab", "a**b", True),
    "infix pair does not recurse": ("ax/xb", "a**b", False),
    "infix pair does not cross one level": ("a/b", "a**b", False),
    "suffixed pair matches inside a segment": ("zzx", "**x", True),
    "suffixed pair does not recurse": ("z/zx", "**x", False),
    "suffixed pair is not a whole segment": ("a/x", "**x", False),
    "prefixed pair matches inside a segment": ("xzz", "x**", True),
    "prefixed pair does not recurse": ("xz/z", "x**", False),
    "prefixed pair is not a whole segment": ("x/z", "x**", False),
}


class PathGlobSegmentContainmentTests(unittest.TestCase):
    """OBL-GLOBS-030."""

    def test_wildcards_stay_inside_a_segment_and_only_a_bare_star_star_recurses(self):
        for label, (path, pattern, expected) in SEGMENT_CASES.items():
            with self.subTest(case=label, path=path, pattern=pattern):
                self.assertIs(_match_path_glob(path, pattern), expected)

    def test_the_recursive_and_ordinary_readings_would_disagree_here(self):
        """The premise: the partial-segment cases are discriminating.

        If ``a**b`` were read as a recursive wildcard it would match
        ``ax/xb``, and if ``**`` were an ordinary star the whole-segment cases
        would stop spanning. Both readings are asserted above, so a matcher
        that confused them fails on one side or the other. This test only
        proves the two readings really differ on the chosen inputs.
        """
        self.assertIs(_match_path_glob("ax/xb", "a*/*b"), True)
        self.assertIs(_match_path_glob("a/x/y/b", "a/*/b"), False)

    def test_the_matcher_is_reached_at_all_for_these_shapes(self):
        """The premise for every False above: a False is an answer, not a refusal.

        ``_match_path_glob`` returns False for a malformed pattern too, so at
        least one True has to come out of each shape under test.
        """
        for pattern in ("src/*.py", "a?b", "a[0-9]b", "a/**/b", "a**b", "**x", "x**"):
            with self.subTest(pattern=pattern):
                self.assertTrue(
                    any(
                        expected and candidate_pattern == pattern
                        for _path, candidate_pattern, expected in SEGMENT_CASES.values()
                    ),
                    f"no positive case exercises {pattern!r}",
                )


# ---------------------------------------------------------------------------
# OBL-GLOBS-053: the cached operation must answer what the one-shot API does
# ---------------------------------------------------------------------------

#: Pattern segments spanning every compiled part kind: literal, single-segment
#: glob (star, question mark, bracket class) and the recursive wildcard, plus
#: the partial-segment ``**`` shapes whose compiled form is a text glob.
PATTERN_SEGMENTS = ("a", "b", "x.py", "*", "*.py", "?", "[ab]", "**", "a**b", "**x")

#: Candidate segments chosen to hit and miss each of those.
PATH_SEGMENTS = ("a", "b", "c", "x.py", "y.py", "ab", "axb", "zzx", "1")

_patterns = st.lists(
    st.sampled_from(PATTERN_SEGMENTS), min_size=1, max_size=4
).map("/".join)
_paths = st.lists(
    st.sampled_from(PATH_SEGMENTS), min_size=1, max_size=4
).map("/".join)
_pairs = st.lists(st.tuples(_paths, _patterns), min_size=1, max_size=8)

#: One operation, several structurally different patterns, run in this order.
#: The point is cross-pattern contamination: pattern four is answered after
#: three others have been compiled into the same cache and charged to the same
#: budget.
INTERLEAVED: Tuple[Tuple[str, str], ...] = (
    ("api/v1.yaml", "api/*.yaml"),
    ("api/v1/v2.yaml", "api/**/*.yaml"),
    ("api/ab.yaml", "api/[ab]b.yaml"),
    ("api/xb.yaml", "api/?b.yaml"),
    ("api/v1.yaml", "api/**"),
    ("api/v1.yaml", "api/*.yaml"),
    ("api/v1/v2.yaml", "api/*.yaml"),
    ("api/axxb", "api/a**b"),
    ("api/ax/xb", "api/a**b"),
)


class PathGlobOperationEquivalenceTests(unittest.TestCase):
    """OBL-GLOBS-053."""

    # ---- premises ------------------------------------------------------

    def test_the_cache_is_really_reused(self):
        """The premise: without a cache hit the equivalence proves nothing."""
        operation = _PathGlobOperation("chunk-16")
        first = operation.prepare("a/**/*.py")
        self.assertEqual(operation.compiled_patterns, 1)
        spent_after_compile = operation.steps
        second = operation.prepare("a/**/*.py")
        self.assertIs(first, second)
        self.assertEqual(operation.compiled_patterns, 1)
        self.assertEqual(operation.steps, spent_after_compile)

    def test_a_repeated_match_costs_less_than_the_first(self):
        """The premise, again, through the public entry point.

        ``matches`` is the call the four production sites make, and a cached
        compile has to show up as work not done or the cache is not on the
        path being tested.
        """
        operation = _PathGlobOperation("chunk-16")
        operation.matches("a/b/c.py", "a/**/*.py")
        first = operation.steps
        operation.matches("a/b/c.py", "a/**/*.py")
        repeat = operation.steps - first
        self.assertLess(repeat, first)
        self.assertEqual(operation.compiled_patterns, 1)

    # ---- the obligation ------------------------------------------------

    def test_interleaved_patterns_each_answer_what_the_one_shot_api_answers(self):
        operation = _PathGlobOperation("chunk-16")
        for index, (path, pattern) in enumerate(INTERLEAVED):
            with self.subTest(step=index, path=path, pattern=pattern):
                self.assertIs(
                    operation.matches(path, pattern),
                    _match_path_glob(path, pattern),
                )
        self.assertEqual(
            operation.compiled_patterns,
            len({pattern for _path, pattern in INTERLEAVED}),
        )

    @PROFILE
    @given(pairs=_pairs, descendants=st.booleans())
    def test_one_shared_operation_agrees_with_the_uncached_api(
        self, pairs, descendants
    ):
        operation = _PathGlobOperation("chunk-16")
        for path, pattern in pairs:
            expected = _match_path_glob(
                path, pattern, _allow_descendants=descendants
            )
            self.assertIs(
                operation.matches(
                    path, pattern, allow_descendants=descendants
                ),
                expected,
            )
        self.assertEqual(
            operation.compiled_patterns,
            len({pattern for _path, pattern in pairs}),
        )

    @PROFILE
    @given(pairs=_pairs)
    def test_a_cold_operation_gives_the_same_answer_as_a_warm_one(self, pairs):
        warm = _PathGlobOperation("chunk-16")
        for path, pattern in pairs:
            cold = _PathGlobOperation("chunk-16")
            self.assertIs(warm.matches(path, pattern), cold.matches(path, pattern))

    # ---- invalid declarations fail closed everywhere -------------------

    def test_an_invalid_pattern_fails_closed_in_every_entry_point(self):
        for pattern in ("a//b", "/abs", "a/", "a/./b", "a/../b"):
            with self.subTest(pattern=pattern):
                with self.assertRaises(GuardrailError) as one_shot:
                    _match_path_glob("a/b", pattern)
                self.assertEqual(
                    str(one_shot.exception),
                    f"Glob match failed closed: invalid path glob {pattern!r}",
                )
                operation = _PathGlobOperation("chunk-16")
                with self.assertRaises(GuardrailError) as raised:
                    operation.matches("a/b", pattern)
                self.assertEqual(
                    str(raised.exception),
                    f"chunk-16 failed closed: invalid path glob {pattern!r}",
                )

    def test_an_empty_pattern_fails_closed_in_both_entry_points(self):
        operation = _PathGlobOperation("chunk-16")
        with self.assertRaises(GuardrailError):
            operation.matches("a/b", "")
        with self.assertRaises(GuardrailError):
            _match_path_glob("a/b", "")

    def test_matches_and_the_one_shot_api_both_refuse_invalid_patterns(self):
        operation = _PathGlobOperation("chunk-16")
        with self.assertRaises(GuardrailError):
            operation.matches("a/b", "a//b")
        with self.assertRaises(GuardrailError):
            _match_path_glob("a/b", "a//b")


# ---------------------------------------------------------------------------
# OBL-HASHING-026 and OBL-HASHING-095: the unframed compat concatenation
# ---------------------------------------------------------------------------

#: Every accepted ``defaults.compat_mode`` and the identity it selects for the
#: version ``1.2.3``. Read from the schema error the fixture below provokes,
#: so a fourth mode makes that test fail rather than sit unexercised.
COMPAT_MODES: Dict[str, str] = {
    "major": "1",
    "semver_major": "1",
    "semver_major_minor": "1.2",
}

#: Component names that are accepted today and that carry the delimiter the
#: compat construction uses. Each maps to the exact string the digest is taken
#: over for identity ``1``.
DELIMITER_NAMES: Dict[str, str] = {
    "svc": "svc@compat:1",
    "svc@compat:1": "svc@compat:1@compat:1",
    "a@compat:b": "a@compat:b@compat:1",
}


class CompatDigestConstructionTests(unittest.TestCase):
    """OBL-HASHING-026 and OBL-HASHING-095."""

    def _scene(
        self,
        name: str = "svc",
        version: str = "1.2.3",
        mode: str = "major",
    ) -> Scenario:
        scene = Scenario()
        scene.component(
            name,
            path="svc",
            provider="leaf",
            version_source={"file": "version.json", "field": "version"},
        )
        scene.defaults(compat_mode=mode)
        scene.json_file("svc/version.json", {"version": version})
        scene.commit()
        return scene

    @staticmethod
    def _expected(name: str, identity: str) -> str:
        """SHA-256 of the concatenation, computed without boundver's helper."""
        return hashlib.sha256(
            f"{name}@compat:{identity}".encode("utf-8")
        ).hexdigest()

    # ---- premises ------------------------------------------------------

    def test_the_fixture_produces_a_compat_digest_at_all(self):
        with self._scene() as scene:
            fingerprints = scene.generate()["components"]["svc"]["fingerprints"]
            self.assertIsNotNone(fingerprints["compat"])
            self.assertEqual(len(fingerprints["compat"]), 64)

    def test_the_mode_table_is_the_whole_accepted_surface(self):
        """The premise that both branches of the compat_mode split are covered."""
        with self._scene(mode="unsupported") as scene:
            errors = validate_config(scene.config, scene.root)
        self.assertIn(
            "Schema validation error at defaults.compat_mode: 'unsupported' is "
            "not one of ['major', 'semver_major', 'semver_major_minor']",
            errors,
        )
        self.assertEqual(
            sorted(COMPAT_MODES), ["major", "semver_major", "semver_major_minor"]
        )

    def test_a_framed_construction_would_give_a_different_answer(self):
        """The premise that 'no entry framing' is an assertion with content."""
        framed = _hash_framed_entries(
            [("compat", b"1")], domain=HASH_DOMAIN_BOUNDARY
        )
        self.assertNotEqual(framed, self._expected("svc", "1"))
        self.assertNotEqual(
            sha256_hex("svc@compat:1\n"), self._expected("svc", "1")
        )

    # ---- OBL-HASHING-026 -----------------------------------------------

    def test_the_compat_digest_is_sha256_of_the_unframed_concatenation(self):
        for mode, identity in COMPAT_MODES.items():
            with self.subTest(compat_mode=mode):
                with self._scene(mode=mode) as scene:
                    entry = scene.generate()["components"]["svc"]
                self.assertEqual(
                    entry["fingerprints"]["compat"], self._expected("svc", identity)
                )
                self.assertEqual(entry["semver"]["compat_family"], "1")
                self.assertEqual(entry["semver"]["api_surface"], "1.2")

    def test_the_two_compat_modes_select_different_identities(self):
        """Otherwise the table above would prove one branch, not two."""
        self.assertNotEqual(
            self._expected("svc", COMPAT_MODES["major"]),
            self._expected("svc", COMPAT_MODES["semver_major_minor"]),
        )

    def test_the_pair_the_obligation_names_does_not_collide(self):
        """``svc`` with ``1`` against ``svc@compat:1`` with an empty identity."""
        left = sha256_hex(f"{'svc'}@compat:{'1'}")
        right = sha256_hex(f"{'svc@compat:1'}@compat:{''}")
        self.assertEqual(
            left, "2b76e797e89bf0561229aa6249cdc899acef599712c928f7824203cbc93d644d"
        )
        self.assertEqual(
            right, "218bfc32b62d69c76de52117351c076908caa01a9a96e0c429e19fe8354d6a09"
        )
        self.assertNotEqual(left, right)

    # ---- OBL-HASHING-095 -----------------------------------------------

    def test_the_shifted_delimiter_pair_does_collide(self):
        """Recorded, not repaired: the construction is genuinely not prefix-free."""
        left = sha256_hex(f"{'a'}@compat:{'b@compat:c'}")
        right = sha256_hex(f"{'a@compat:b'}@compat:{'c'}")
        self.assertEqual(left, right)
        self.assertEqual(
            left, "c6f6ff256a5afcdef0895474d4dd8d751f3ddb69f189f2d1ef9048bbd95b41ff"
        )

    def test_a_component_name_may_carry_the_delimiter_today(self):
        for name, concatenation in DELIMITER_NAMES.items():
            with self.subTest(name=name):
                with self._scene(name=name) as scene:
                    errors = validate_config(scene.config, scene.root)
                    fingerprints = scene.generate()["components"][name][
                        "fingerprints"
                    ]
                self.assertEqual(errors, [])
                self.assertEqual(
                    fingerprints["compat"],
                    hashlib.sha256(concatenation.encode("utf-8")).hexdigest(),
                )

    def test_two_delimiter_bearing_names_still_get_distinct_digests(self):
        digests = {
            name: hashlib.sha256(concatenation.encode("utf-8")).hexdigest()
            for name, concatenation in DELIMITER_NAMES.items()
        }
        self.assertEqual(len(set(digests.values())), len(DELIMITER_NAMES))

    def test_a_configured_version_can_produce_an_identity_at_all(self):
        """The premise for the property below: it is not vacuously true."""
        self.assertEqual(parse_semver("1.2.3"), ("1", "1.2", "1.2.3"))
        self.assertEqual(parse_semver("v10.20.30-rc.1"), ("10", "10.20", "10.20.30"))

    @PROFILE
    @given(
        version=st.text(
            alphabet="0123456789.v-+@compat:xyz", min_size=0, max_size=24
        )
    )
    def test_no_configured_version_can_put_the_delimiter_in_the_identity(self, version):
        """The barrier that makes the recorded collision unreachable.

        The name half of the concatenation is unconstrained, so the only thing
        preventing two components from sharing a compat fingerprint is that
        the identity half is whatever ``parse_semver`` returns for the major
        or major.minor core. The oracle is character membership, which knows
        nothing about how that parser is written.
        """
        compat, api_surface, _exact = parse_semver(version)
        for identity in (compat, api_surface):
            if identity is None:
                continue
            self.assertEqual(set(identity) - set("0123456789."), set())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
