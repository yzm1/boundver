"""A setting that means "off" on one Git and "run this program" on another.

`core.fsmonitor` began life as the pathname of a hook program and only later
learned to accept booleans, so the string `false` that boundver hands to every
Git subprocess is a boolean on a current Git and a program name on an old one.
boundver passes the inherited PATH through untouched, so on such a Git a file
named `false` sitting early on PATH would be executed by the very setting meant
to switch the monitor off. Git's own documentation names the hazard: versions
2.35.1 and earlier "will consider the 'true' or 'false' values as hook pathnames
to be invoked".

Proving the program never runs is awkward for several reasons. A negative is
worth nothing unless the same mechanism can be shown firing, and only some Git
subcommands consult the hook at all: `write-tree`, `ls-files --cached` and
`diff --cached` do, while `rev-parse`, `ls-tree` and `config` never ask. A
marker test aimed at code that happens to use only the second group passes no
matter what boundver does. The kill switch also travels twice, once as
`-c core.fsmonitor=false` on the argv and once as the `GIT_CONFIG_KEY_1` pair in
the environment, and either carrier alone suffices, so deleting one of them
changes nothing a behavioural test can see.

The third difficulty is the value itself. On the Git this file insists on, a
repository whose own `core.fsmonitor` says `false` is handing Git a boolean, so
Git never resolves it as a program and a marker of that name cannot run however
badly boundver behaves. The obligation is worded in terms of `false`, so the row
stays, but it is a scope pin for a sub-floor Git rather than a witness here, and
`TheLiteralFalseIsInertHereTests` measures that inertness instead of leaving a
reader to assume teeth. Every live measurement uses `bvmarker`, a name no Git
can read as a boolean.

The fixture answers all of it. It builds a recording program under three
spellings, puts its directory first on PATH, and has the repository under test
name that program as its fsmonitor hook. Every "the program did not run"
assertion here is preceded, in the same test, by one that fires the same program
through the same PATH under the same runner: raw Git for the raw-Git claims, the
same in-process command with both carriers patched out for the in-process
claims, and - for the end-to-end claim, which crosses a process boundary that
`mock.patch` cannot - the same real `python -m boundver` subprocess with both
carriers stripped inside the child by a generated `sitecustomize`. The argv and
the environment pair are pinned structurally alongside, because the behavioural
assertions cannot notice either carrier disappearing on its own.

Covers OBL-GIT-SOURCE-157.
"""

from __future__ import annotations

import contextlib
import inspect
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple
from unittest import mock

from boundver import _git as git
from boundver._git import (
    _ambient_worktree_config_overrides,
    _git_subprocess_env,
    _offline_git_command,
    _repository_filter_config_overrides,
)

from tests._parity import run_cli, run_cli_in_process
from tests._scenarios import Scenario

#: The kill switch as it is spelled on an argv and as a config key/value pair.
FSMONITOR_FLAG = "core.fsmonitor=false"
FSMONITOR_KEY = "core.fsmonitor"
FSMONITOR_VALUE = "false"

#: Git learned to read core.fsmonitor as a boolean in 2.35.2. Below that floor
#: the value `false` is a hook pathname, and nothing observed on this host says
#: anything about the hazard the obligation names.
BOOLEAN_FSMONITOR_FLOOR = (2, 35, 2)

#: The value every live measurement here uses. No Git reads `bvmarker` as a
#: boolean and no shell provides it as a builtin, so it reaches hook mode on
#: every Git and makes the fixture's own machinery observable.
LIVE_FSMONITOR_VALUE = "bvmarker"

#: The obligation's literal wording: the name a Git below the boolean floor
#: would spawn. At or above the floor Git parses it instead, so a repository
#: carrying it cannot spawn anything and no assertion about it has teeth here.
LITERAL_FSMONITOR_VALUE = "false"

#: A name that every POSIX shell provides as a builtin. If Git handed a bare
#: hook name to a shell rather than resolving it against PATH, a marker of this
#: name could not fire, and the whole PATH premise would be wishful.
BUILTIN_FSMONITOR_VALUE = "echo"

#: Names the recording program is installed under. All three are installed for
#: every test, so "the marker fired" is never ambiguous about which name Git
#: resolved: exactly one of the three records may appear.
MARKER_NAMES = (LIVE_FSMONITOR_VALUE, LITERAL_FSMONITOR_VALUE, BUILTIN_FSMONITOR_VALUE)

#: Git subcommands boundver issues that do consult the fsmonitor hook, in the
#: argv shape boundver issues them. These make the fixture itself checkable.
FSMONITOR_READING_CALLS = {
    "write-tree": ("write-tree",),
    "ls-files --cached": (
        "--literal-pathspecs",
        "ls-files",
        "--cached",
        "-t",
        "-z",
        "--",
    ),
    "diff --cached": (
        "diff",
        "--cached",
        "--no-ext-diff",
        "--no-textconv",
        "--ignore-submodules=dirty",
    ),
}

#: Git subcommands boundver issues that never consult the hook. Code built only
#: from these cannot witness the claim, however hostile the repository.
FSMONITOR_BLIND_CALLS = {
    "rev-parse": ("rev-parse", "--verify", "--quiet", "HEAD^{commit}"),
    "ls-tree": ("ls-tree", "-r", "-z", "--full-tree", "HEAD"),
    "config": ("config", "--bool", "core.filemode"),
}

#: One legal argv per allowlisted subcommand, in the shape boundver builds it.
#: The builder inserts the kill switch ahead of the subcommand, so every one of
#: these has to come back carrying it.
ALLOWLISTED_CALLS = {
    "cat-file": ["cat-file", "--batch"],
    "check-ref-format": ["check-ref-format", "refs/heads/main"],
    "config": ["config", "--bool", "core.filemode"],
    "describe": ["describe", "--tags"],
    "diff": ["diff", "--cached"],
    "diff-tree": ["diff-tree", "-r", "HEAD~1", "HEAD"],
    "ls-files": ["--literal-pathspecs", "ls-files", "--cached", "-t", "-z", "--"],
    "ls-tree": ["ls-tree", "-r", "-z", "--full-tree", "HEAD"],
    "merge-base": ["merge-base", "HEAD", "HEAD"],
    "rev-list": ["rev-list", "--first-parent", "HEAD"],
    "rev-parse": ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
    "show-ref": ["show-ref", "--verify", "refs/heads/main"],
    "symbolic-ref": ["symbolic-ref", "--short", "HEAD"],
    "version": ["version"],
    "write-tree": ["write-tree"],
    "with a safe.directory prefix": [
        "-c",
        "safe.directory=/anywhere",
        "config",
        "--bool",
        "core.filemode",
    ],
}

#: The one key in ALLOWLISTED_CALLS that names an argv shape rather than a
#: subcommand. Every other key has to be a subcommand src/ actually allows.
ARGV_SHAPE_KEY = "with a safe.directory prefix"

#: The commands the claim is made about, and the exit code each returns against
#: the fixture repository. They run in this order in one repository, so the
#: first `generate` writes the lockfile the last two read. Head mode belongs
#: here despite reading only committed objects, because the CLI additionally
#: checks the working tree to warn about uncommitted changes.
CLEAN_COMMANDS = {
    "generate --source working-tree": (
        ("generate", "--source", "working-tree"),
        0,
    ),
    "generate --source index": (("generate", "--source", "index"), 0),
    "generate --source head": (("generate", "--source", "head"), 0),
    "status --source working-tree": (("status", "--source", "working-tree"), 0),
    "explain --source working-tree": (
        ("explain", "svc", "--source", "working-tree"),
        0,
    ),
}

#: verify's exit code when it finds drift, which is what the fixture arranges.
VERIFY_DRIFT_EXIT = 4

POSIX_MARKER = "#!/bin/sh\nprintf 'ran\\n' >> '{record}'\nexit 1\n"
WINDOWS_MARKER = '@echo off\r\necho ran>> "{record}"\r\nexit /b 1\r\n'


def _installed_git_version() -> Tuple[int, ...]:
    """The running Git's version triple, or () when it cannot be read."""
    try:
        reported = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover
        return ()
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", reported)
    return tuple(int(part) for part in found.groups()) if found else ()


GIT_VERSION = _installed_git_version()


class RecordingPrograms:
    """Programs on a PATH of their own that record the fact they were run.

    Three spellings of each name are installed because which one wins is the
    host's decision rather than ours: an extensionless `#!/bin/sh` script is
    what a POSIX host resolves, and Git for Windows may take that or a `.bat`
    or `.cmd` of the same name depending on PATHEXT. Each spelling writes to a
    differently named record, so a test can report which one ran without
    depending on any of them in particular.
    """

    def __init__(self, names: Iterable[str] = MARKER_NAMES) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.programs = self.root / "bin"
        self.records = self.root / "records"
        self.programs.mkdir()
        self.records.mkdir()
        for name in names:
            self._install(name)
        self.path = str(self.programs) + os.pathsep + os.environ["PATH"]

    def _install(self, name: str) -> None:
        posix = self.programs / name
        posix.write_bytes(
            POSIX_MARKER.format(
                record=(self.records / f"{name}.noext").as_posix()
            ).encode("utf-8")
        )
        os.chmod(posix, 0o755)
        for suffix in (".bat", ".cmd"):
            script = self.programs / f"{name}{suffix}"
            script.write_bytes(
                WINDOWS_MARKER.format(
                    record=str(self.records / f"{name}{suffix}")
                ).encode("utf-8")
            )

    def fired(self) -> List[str]:
        """Which recording programs have run since the last `forget`."""
        return sorted(entry.name for entry in self.records.iterdir())

    def fired_names(self) -> List[str]:
        """Which *names* fired, with the host's chosen spelling dropped."""
        return sorted({record.rsplit(".", 1)[0] for record in self.fired()})

    def forget(self) -> None:
        for entry in self.records.iterdir():
            entry.unlink()

    def close(self) -> None:
        self._directory.cleanup()


def _repository(fsmonitor: str = LIVE_FSMONITOR_VALUE) -> Scenario:
    """A committed component, a dirty working tree, a hostile fsmonitor hook.

    The working tree has to differ from the index for a refresh to have real
    work to do, and the hook is configured last so that none of the fixture's
    own Git calls run under it.
    """
    scene = Scenario()
    scene.component(
        "svc", path="services/svc", provider="path-hash", boundary=["api/*.yaml"]
    )
    scene.file("services/svc/api/v1.yaml", "openapi: 3.1.0\n")
    scene.commit()
    scene.append_line("services/svc/api/v1.yaml", "# uncommitted")
    scene.git("config", "core.fsmonitor", fsmonitor)
    return scene


def _raw_git(
    scene: Scenario, marker: RecordingPrograms, arguments: Sequence[str]
) -> subprocess.CompletedProcess:
    """Run Git the way boundver does, minus every one of boundver's overrides."""
    return subprocess.run(
        [
            "git",
            "-C",
            str(scene.root),
            f"--work-tree={scene.root}",
            "--no-pager",
            *arguments,
        ],
        cwd=scene.root,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": marker.path},
    )


def _without_flag(command: List[str]) -> List[str]:
    """The same argv with the `-c core.fsmonitor=false` pair taken out."""
    kept: List[str] = []
    index = 0
    while index < len(command):
        if command[index] == "-c" and command[index + 1 : index + 2] == [
            FSMONITOR_FLAG
        ]:
            index += 2
            continue
        kept.append(command[index])
        index += 1
    return kept


def _config_pairs(environment: Mapping[str, str]) -> List[Tuple[str, str]]:
    """The GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n block, in declared order."""
    count = int(environment["GIT_CONFIG_COUNT"])
    return [
        (
            environment[f"GIT_CONFIG_KEY_{index}"],
            environment[f"GIT_CONFIG_VALUE_{index}"],
        )
        for index in range(count)
    ]


def _without_config_pair(environment: Mapping[str, str]) -> Dict[str, str]:
    """The same environment with core.fsmonitor gone and the block renumbered.

    Renumbering is not tidiness. Git refuses to start when GIT_CONFIG_COUNT
    names an index whose key variable is missing, so a hole would abort the
    process instead of leaving the hook live, and the mutation would prove
    nothing.
    """
    result = dict(environment)
    count = int(result.get("GIT_CONFIG_COUNT", "0"))
    kept: List[Tuple[str, str]] = []
    for index in range(count):
        key = result.pop(f"GIT_CONFIG_KEY_{index}", None)
        value = result.pop(f"GIT_CONFIG_VALUE_{index}", None)
        if key is not None and value is not None and key != FSMONITOR_KEY:
            kept.append((key, value))
    for index, (key, value) in enumerate(kept):
        result[f"GIT_CONFIG_KEY_{index}"] = key
        result[f"GIT_CONFIG_VALUE_{index}"] = value
    result["GIT_CONFIG_COUNT"] = str(len(kept))
    return result


@contextlib.contextmanager
def _carriers_removed(*, drop_flag: bool, drop_config: bool):
    """Run boundver with one or both carriers of the kill switch taken out.

    Both are patched at the single choke point every Git subprocess passes
    through, so this reaches the blob transport and the batch session as well
    as the ordinary run helper.
    """
    real_command = git._offline_git_command
    real_environment = git._offline_git_environment

    def build_command(repo_root, args):
        command = real_command(repo_root, args)
        return _without_flag(command) if drop_flag else command

    def build_environment(repo_root=None, environment=None):
        built = real_environment(repo_root, environment)
        return _without_config_pair(built) if drop_config else built

    with mock.patch.object(git, "_offline_git_command", build_command):
        with mock.patch.object(git, "_offline_git_environment", build_environment):
            yield


#: The tail of the generated `sitecustomize`: it installs the two mutations
#: this module already applies in-process, at the same choke point.
_CHILD_SHIM_TAIL = """
_real_command = _git._offline_git_command
_real_environment = _git._offline_git_environment


def _stripped_command(repo_root, args):
    return _without_flag(_real_command(repo_root, args))


def _stripped_environment(repo_root=None, environment=None):
    return _without_config_pair(_real_environment(repo_root, environment))


_git._offline_git_command = _stripped_command
_git._offline_git_environment = _stripped_environment
"""


def _child_shim_source() -> str:
    """A `sitecustomize` that strips both carriers inside another interpreter.

    The end-to-end claim is made against a real `python -m boundver`, and a
    negative asserted there needs its positive control asserted there too - but
    `mock.patch` does not cross a process boundary. Python imports
    `sitecustomize` from PYTHONPATH at startup, and `run_cli` extends PYTHONPATH
    rather than replacing it, so a shim placed there runs before the CLI does.
    The two mutation functions are lifted out of this module by
    `inspect.getsource`, so the child and the parent cannot drift apart.
    """
    return "\n".join(
        [
            "from __future__ import annotations",
            "",
            "from boundver import _git",
            "",
            f"FSMONITOR_FLAG = {FSMONITOR_FLAG!r}",
            f"FSMONITOR_KEY = {FSMONITOR_KEY!r}",
            "",
            inspect.getsource(_without_flag),
            "",
            inspect.getsource(_without_config_pair),
            _CHILD_SHIM_TAIL,
        ]
    )


@contextlib.contextmanager
def _carriers_removed_in_child():
    """Yield a directory holding that shim, for the child's PYTHONPATH."""
    with tempfile.TemporaryDirectory() as shim_root:
        Path(shim_root, "sitecustomize.py").write_text(
            _child_shim_source(), encoding="utf-8"
        )
        yield shim_root


class _CacheDiscipline(unittest.TestCase):
    """Both `lru_cache`d config readers, cleared on the way in and the way out.

    The whole suite runs in one process, so a tuple cached under a marker PATH
    or a patched choke point would otherwise be handed to a later file.
    """

    def setUp(self):
        _ambient_worktree_config_overrides.cache_clear()
        _repository_filter_config_overrides.cache_clear()

    def tearDown(self):
        _ambient_worktree_config_overrides.cache_clear()
        _repository_filter_config_overrides.cache_clear()


class _MarkerCase(_CacheDiscipline):
    """Shared fixture: a marker PATH and a repository that names the marker."""

    fsmonitor = LIVE_FSMONITOR_VALUE

    def setUp(self):
        super().setUp()
        self.marker = RecordingPrograms()
        self.scene = _repository(self.fsmonitor)

    def tearDown(self):
        self.scene.close()
        self.marker.close()
        super().tearDown()

    @contextlib.contextmanager
    def marker_on_path(self):
        """PATH is restored on the way out; the suite shares one process."""
        self.marker.forget()
        with mock.patch.dict(os.environ, {"PATH": self.marker.path}, clear=False):
            yield


class GitParsesTheKillSwitchAsABooleanTests(unittest.TestCase):
    """OBL-GIT-SOURCE-157: what `false` means to the Git actually installed."""

    def test_the_installed_git_is_at_or_above_the_boolean_handling_floor(self):
        """Below it `false` is a pathname, and every negative here is empty."""
        self.assertNotEqual(GIT_VERSION, (), "git --version could not be read")
        self.assertGreaterEqual(
            GIT_VERSION,
            BOOLEAN_FSMONITOR_FLOOR,
            "core.fsmonitor gained boolean handling in Git "
            f"{'.'.join(str(part) for part in BOOLEAN_FSMONITOR_FLOOR)}; on this "
            f"Git ({'.'.join(str(part) for part in GIT_VERSION)}) the value "
            f"{FSMONITOR_VALUE!r} names a program to run",
        )

    def test_false_parses_as_a_boolean_and_a_bare_name_does_not(self):
        """The discriminator: --type=bool refuses anything that is a pathname."""
        boolean = subprocess.run(
            ["git", "-c", FSMONITOR_FLAG, "config", "--type=bool", FSMONITOR_KEY],
            capture_output=True,
            text=True,
        )
        self.assertEqual(boolean.returncode, 0, boolean.stderr)
        self.assertEqual(boolean.stdout.strip(), FSMONITOR_VALUE)

        name = LIVE_FSMONITOR_VALUE
        pathname = subprocess.run(
            [
                "git",
                "-c",
                f"{FSMONITOR_KEY}={name}",
                "config",
                "--type=bool",
                FSMONITOR_KEY,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(pathname.returncode, 128)
        self.assertEqual(
            pathname.stderr.strip(),
            f"fatal: bad boolean config value '{name}' for '{FSMONITOR_KEY}'",
        )


class RecordingProgramPremiseTests(_MarkerCase):
    """OBL-GIT-SOURCE-157: the marker fires, so its later silence means something."""

    def test_raw_git_runs_the_program_the_repository_names(self):
        """Hook mode is live here, PATH resolves it, and the markers work."""
        for label, arguments in FSMONITOR_READING_CALLS.items():
            with self.subTest(subcommand=label):
                self.marker.forget()
                result = _raw_git(self.scene, self.marker, arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotEqual(
                    self.marker.fired(),
                    [],
                    f"{label} did not consult the fsmonitor hook, so every "
                    "assertion resting on it would pass vacuously",
                )

    def test_a_subcommand_that_never_consults_the_hook_stays_silent(self):
        """Silence is not universal, which is what makes the negatives content.

        The return code is asserted first and deliberately. A subcommand that
        died before it reached the index - a HEAD that stopped resolving, a
        flag a future Git rejects - would leave the same empty record, and this
        control would decay from "silent because it never asks" into "silent
        because nothing ran".
        """
        for label, arguments in FSMONITOR_BLIND_CALLS.items():
            with self.subTest(subcommand=label):
                self.marker.forget()
                result = _raw_git(self.scene, self.marker, arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.marker.fired(), [])

    @unittest.skipIf(
        os.name == "nt",
        "Git for Windows may dispatch a bare fsmonitor name through its shell",
    )
    def test_git_resolves_a_bare_hook_name_against_path_rather_than_a_shell(self):
        """A shell builtin does not shadow the PATH program Git is told to run.

        `false` is a builtin in every POSIX shell, so if Git passed the hook
        name to a shell instead of resolving it itself, the whole PATH premise
        would be wishful and the obligation's hazard could not exist as stated.
        The measurement uses `echo`, which is a builtin in the same shells but,
        unlike `false`, is not a boolean to any Git, so hook mode is reachable
        on this host. Both names are installed in one directory and the two
        repositories differ only in which one they configure, so each arm also
        shows that the record which appears is the one the config asked for.
        """
        for configured in (LIVE_FSMONITOR_VALUE, BUILTIN_FSMONITOR_VALUE):
            with self.subTest(configured=configured):
                with _repository(configured) as scene:
                    self.marker.forget()
                    result = _raw_git(scene, self.marker, ("write-tree",))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        self.marker.fired_names(),
                        [configured],
                        "the program Git ran was not the one the repository "
                        f"named: {self.marker.fired()}",
                    )


class TheLiteralFalseIsInertHereTests(_MarkerCase):
    """OBL-GIT-SOURCE-157: the scope of the obligation's own literal wording.

    The obligation is written about a repository whose `core.fsmonitor` is the
    string `false`, and this file keeps that row. But at or above
    BOOLEAN_FSMONITOR_FLOOR - which the version test above insists on - Git
    parses that string instead of resolving it, so no program of that name can
    run whatever boundver does, and an assertion of silence there is empty. The
    tests here measure that emptiness rather than leaving it to be assumed, so
    the row below reads as the sub-floor scope pin it is and not as a second
    live witness.
    """

    fsmonitor = LITERAL_FSMONITOR_VALUE

    def test_raw_git_spawns_nothing_for_false_but_does_for_a_bare_name(self):
        """The same call, the same PATH, the same marker: only the name differs."""
        for label, arguments in FSMONITOR_READING_CALLS.items():
            with self.subTest(subcommand=label):
                self.marker.forget()
                inert = _raw_git(self.scene, self.marker, arguments)
                self.assertEqual(inert.returncode, 0, inert.stderr)
                self.assertEqual(
                    self.marker.fired(),
                    [],
                    f"a repository-local {FSMONITOR_KEY}={LITERAL_FSMONITOR_VALUE} "
                    f"spawned a program on Git "
                    f"{'.'.join(str(part) for part in GIT_VERSION)}, which is at "
                    "or above the boolean floor and should have parsed it",
                )

                with _repository(LIVE_FSMONITOR_VALUE) as live:
                    self.marker.forget()
                    spawned = _raw_git(live, self.marker, arguments)
                    self.assertEqual(spawned.returncode, 0, spawned.stderr)
                    self.assertEqual(
                        self.marker.fired_names(), [LIVE_FSMONITOR_VALUE]
                    )

    def test_boundver_is_silent_there_too_though_that_costs_git_nothing(self):
        """A scope pin, not a witness, and named so it cannot be miscounted.

        Nothing boundver could do would make this fail on a Git at or above the
        floor, because the previous test shows raw Git itself spawns nothing.
        It is kept because on a sub-floor Git - the host the obligation is
        actually about - this same row becomes a real measurement, and because
        deleting it would quietly narrow the file's scope to `bvmarker`.
        """
        arguments, expected = CLEAN_COMMANDS["generate --source working-tree"]
        with self.marker_on_path():
            result = run_cli(self.scene.root, *arguments)
        self.assertEqual(result.returncode, expected, result.stderr)
        self.assertEqual(self.marker.fired(), [])


class HookReachabilityPremiseTests(_MarkerCase):
    """OBL-GIT-SOURCE-157: every command claimed clean does reach the hook.

    These run the CLI inside this interpreter rather than as a subprocess,
    because removing a carrier is an in-process patch. The end-to-end claim in
    `HostileRepositoryTests` carries its own positive control across the process
    boundary, so this class is about the direct entry points rather than a stand
    -in for the subprocess.
    """

    def _stripped(self, arguments: Sequence[str]):
        with self.marker_on_path():
            with _carriers_removed(drop_flag=True, drop_config=True):
                return run_cli_in_process(self.scene.root, *arguments)

    def test_every_command_reaches_the_hook_once_the_switch_is_removed(self):
        for label, (arguments, expected) in CLEAN_COMMANDS.items():
            with self.subTest(command=label):
                result = self._stripped(arguments)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertNotEqual(
                    self.marker.fired(),
                    [],
                    f"{label} never consulted the fsmonitor hook even with the "
                    "kill switch removed, so asserting its silence proves nothing",
                )

    def test_verify_reaches_the_hook_once_the_switch_is_removed(self):
        with self.marker_on_path():
            written = run_cli_in_process(
                self.scene.root, "generate", "--source", "working-tree"
            )
        self.assertEqual(written.returncode, 0, written.stderr)
        self.scene.append_line("services/svc/api/v1.yaml", "# drifted further")
        result = self._stripped(("verify", "--source", "working-tree"))
        self.assertEqual(result.returncode, VERIFY_DRIFT_EXIT, result.stderr)
        self.assertNotEqual(self.marker.fired(), [])

    def test_the_head_mode_digest_walk_alone_is_blind_where_the_others_are_not(self):
        """Head mode reads committed objects only, and the contrast is measured.

        `generate --source head` at the CLI does fire the marker above, but only
        because the CLI inspects the working tree to warn about uncommitted
        changes. The walk underneath it touches nothing the hook is consulted
        for. Asserting that alone would be worthless without showing that this
        very entry point - `generate_lockfile`, not the CLI - can fire the
        marker at all, so the working-tree and index sources are driven through
        the same call under the same patch in the same test.
        """
        fired: Dict[str, List[str]] = {}
        for source in ("working-tree", "index", "head"):
            with self.marker_on_path():
                with _carriers_removed(drop_flag=True, drop_config=True):
                    self.scene.generate(source=source)
            fired[source] = self.marker.fired()
        self.assertNotEqual(fired["working-tree"], [], fired)
        self.assertNotEqual(fired["index"], [], fired)
        self.assertEqual(fired["head"], [], fired)


class RedundantCarrierTests(_MarkerCase):
    """OBL-GIT-SOURCE-157: either carrier on its own disables the hook."""

    def test_dropping_one_carrier_changes_nothing_observable(self):
        """Which is why the argv and the environment are pinned separately below.

        The third row is the premise for the other two. Both single-carrier rows
        assert silence through `generate_lockfile` directly, and silence there
        would also be produced by a refactor that stopped issuing `write-tree`
        or `ls-files --cached` at all. Dropping both carriers in the same table
        must make the same call, on the same fixture, fire the marker.
        """
        removals = {
            "the command-line flag alone": (
                dict(drop_flag=True, drop_config=False),
                True,
            ),
            "the environment pair alone": (
                dict(drop_flag=False, drop_config=True),
                True,
            ),
            "both carriers together": (
                dict(drop_flag=True, drop_config=True),
                False,
            ),
        }
        for label, (removal, expect_silence) in removals.items():
            with self.subTest(removed=label):
                with self.marker_on_path():
                    with _carriers_removed(**removal):
                        self.scene.generate(source="working-tree")
                if expect_silence:
                    self.assertEqual(self.marker.fired(), [])
                else:
                    self.assertNotEqual(
                        self.marker.fired(),
                        [],
                        "this call path never reaches the hook even with both "
                        "carriers gone, so the two rows above assert nothing",
                    )


class HostileRepositoryTests(_CacheDiscipline):
    """OBL-GIT-SOURCE-157: no boundver command spawns what core.fsmonitor names.

    This is the claim itself, made against the real `python -m boundver`
    subprocess a user runs. Each negative is paired inside its own subTest with
    the identical command, in the identical repository, under the identical
    marker PATH and the identical runner, differing only in that a generated
    `sitecustomize` has stripped both carriers inside the child. That run must
    fire the marker. Without it the silence would be evidence about the runner
    rather than about the hardening.
    """

    def setUp(self):
        super().setUp()
        self.marker = RecordingPrograms()

    def tearDown(self):
        self.marker.close()
        super().tearDown()

    def _run(self, root: Path, arguments: Sequence[str], *, stripped: bool = False):
        """The real CLI, in its own process, with the marker first on PATH."""
        self.marker.forget()
        overrides = {"PATH": self.marker.path}
        with contextlib.ExitStack() as stack:
            if stripped:
                overrides["PYTHONPATH"] = stack.enter_context(
                    _carriers_removed_in_child()
                )
            stack.enter_context(mock.patch.dict(os.environ, overrides, clear=False))
            return run_cli(root, *arguments)

    def test_no_command_runs_the_program_the_repository_names(self):
        with _repository(LIVE_FSMONITOR_VALUE) as scene:
            for label, (arguments, expected) in CLEAN_COMMANDS.items():
                with self.subTest(command=label):
                    engaged = self._run(scene.root, arguments, stripped=True)
                    self.assertEqual(engaged.returncode, expected, engaged.stderr)
                    self.assertEqual(
                        self.marker.fired_names(),
                        [LIVE_FSMONITOR_VALUE],
                        f"{label} did not spawn the hook in a real subprocess "
                        "even with both carriers stripped inside the child, so "
                        "its silence below would be evidence about the runner",
                    )

                    result = self._run(scene.root, arguments)
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertEqual(self.marker.fired(), [])

    def test_verify_reports_drift_without_running_the_hook(self):
        """The command most in want of a fresh scan, on a tree that has drifted."""
        with _repository(LIVE_FSMONITOR_VALUE) as scene:
            written = self._run(scene.root, ("generate", "--source", "working-tree"))
            self.assertEqual(written.returncode, 0, written.stderr)
            self.assertTrue((scene.root / "boundary.lock.json").is_file())
            scene.append_line("services/svc/api/v1.yaml", "# drifted further")

            engaged = self._run(
                scene.root, ("verify", "--source", "working-tree"), stripped=True
            )
            self.assertEqual(engaged.returncode, VERIFY_DRIFT_EXIT, engaged.stderr)
            self.assertEqual(self.marker.fired_names(), [LIVE_FSMONITOR_VALUE])

            result = self._run(scene.root, ("verify", "--source", "working-tree"))
            self.assertEqual(result.returncode, VERIFY_DRIFT_EXIT, result.stderr)
            self.assertEqual(self.marker.fired(), [])


class KillSwitchCarrierTests(_CacheDiscipline):
    """OBL-GIT-SOURCE-157: both carriers are present and spelled correctly."""

    @staticmethod
    def _dash_c_values(command: Sequence[str]) -> List[str]:
        return [
            command[index + 1]
            for index in range(len(command) - 1)
            if command[index] == "-c"
        ]

    def test_the_pinned_argv_shapes_cover_every_allowlisted_subcommand(self):
        """A subcommand added to src/ must not slip past the two argv pins."""
        self.assertEqual(
            set(ALLOWLISTED_CALLS) - {ARGV_SHAPE_KEY},
            set(git._OFFLINE_GIT_SUBCOMMANDS),
        )

    def test_every_allowlisted_argv_carries_the_flag_next_to_its_dash_c(self):
        """`core.fsmonitor=false` anywhere else on the argv would be a pathspec."""
        root = Path(__file__).resolve().parent
        for label, arguments in ALLOWLISTED_CALLS.items():
            with self.subTest(call=label):
                command = _offline_git_command(root, list(arguments))
                self.assertEqual(
                    self._dash_c_values(command).count(FSMONITOR_FLAG), 1, command
                )

    def test_the_flag_precedes_the_subcommand_so_git_reads_it_globally(self):
        """A `-c` after the subcommand is an argument to it, not configuration."""
        root = Path(__file__).resolve().parent
        checked = 0
        for subcommand, arguments in ALLOWLISTED_CALLS.items():
            if subcommand not in arguments:
                continue
            with self.subTest(call=subcommand):
                command = _offline_git_command(root, list(arguments))
                self.assertLess(
                    command.index(FSMONITOR_FLAG), command.index(subcommand), command
                )
                checked += 1
        self.assertEqual(
            checked,
            len(ALLOWLISTED_CALLS) - 1,
            "the loop skipped a row it should have measured; only "
            f"{ARGV_SHAPE_KEY!r} names an argv shape rather than a subcommand",
        )

    def test_the_process_environment_carries_the_same_key_and_value(self):
        environment = _git_subprocess_env()
        self.assertEqual(environment["GIT_CONFIG_KEY_1"], FSMONITOR_KEY)
        self.assertEqual(environment["GIT_CONFIG_VALUE_1"], FSMONITOR_VALUE)
        self.assertIn((FSMONITOR_KEY, FSMONITOR_VALUE), _config_pairs(environment))

    def test_a_repository_specific_environment_keeps_the_pair(self):
        """Repository overrides are appended from index 5, never over the pair."""
        with _repository(LIVE_FSMONITOR_VALUE) as scene:
            environment = _git_subprocess_env(scene.root)
            pairs = _config_pairs(environment)
            self.assertIn((FSMONITOR_KEY, FSMONITOR_VALUE), pairs)
            self.assertEqual([key for key, _ in pairs].count(FSMONITOR_KEY), 1, pairs)
            self.assertNotIn(
                f"GIT_CONFIG_KEY_{len(pairs)}",
                environment,
                "GIT_CONFIG_COUNT does not cover every key that was set",
            )

    def test_removing_the_pair_renumbers_rather_than_leaving_a_hole(self):
        """The premise for the mutations above: Git must still start afterwards."""
        with _repository(LIVE_FSMONITOR_VALUE) as scene:
            stripped = _without_config_pair(_git_subprocess_env(scene.root))
            self.assertNotIn(FSMONITOR_KEY, [key for key, _ in _config_pairs(stripped)])
            probe = subprocess.run(
                ["git", "config", "--get", FSMONITOR_KEY],
                cwd=scene.root,
                capture_output=True,
                text=True,
                env=stripped,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertEqual(probe.stdout.strip(), LIVE_FSMONITOR_VALUE)


if __name__ == "__main__":
    unittest.main()
