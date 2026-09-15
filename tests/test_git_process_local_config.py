"""A process-local Git hardening block that is verified before use.

boundver keeps its Git subprocesses safe by pushing configuration into the
process rather than onto the command line. The exact repository path allowed by
``safe.directory``, ``core.hooksPath`` pointed at the null device, and the
computed neutralization of every filter driver a repository declares all travel
in ``GIT_CONFIG_COUNT``, ``GIT_CONFIG_KEY_n`` and ``GIT_CONFIG_VALUE_n``. Git
learned to read those variables in 2.31. An older Git receives them, does not
recognise them, and says nothing, because an unknown environment variable is
not an error, so the whole hardening block is discarded in silence.

The difficulty is that this machine has one Git and it honours the mechanism,
so every statement about a Git that ignores the block rests on a simulation,
and a simulation is worth exactly what its premises are worth. The simulation
here is built at ``_offline_git_environment``, the one function all five
subprocess sites take their environment from, and nothing in this file asserts
an absence without first showing, in the same test, that the same measurement
reports a presence when it should. The clean filter that must not run under the
hardened environment is shown running under the ambient one and again once the
three variable families are stripped; every injected key that reads back is
shown returning nothing when they are; and Git is shown refusing a malformed
count, which is how we know it parses the block rather than skipping it.

Two of those measurements are taken at the process boundary rather than at the
function that builds an environment, because a name is not a value.
``GIT_CONFIG_COUNT`` is present in a block that carries nothing repository
specific, so counting handouts that mention it would accept a package that had
stopped passing the repository root, or that had stopped routing some of its
launches through the choke point at all. What is asserted instead is a property
of every Git process a verify actually starts: apart from the four bootstrap
query kinds, which are named and excused individually, each one receives a
block whose ``safe.directory`` is this repository's resolved path, and each one
receives ``GIT_CONFIG_GLOBAL`` and ``GIT_CONFIG_NOSYSTEM`` so no other scope
can answer.

The suppression of a declared filter is over-determined, and saying so is part
of the measurement. An empty ``filter.<driver>.clean`` stops the driver on its
own and an empty ``filter.<driver>.process`` stops it on its own, while the
``smudge`` and ``required`` overrides do not, so a behavioural row that only
asks whether the filter ran cannot tell which key did the work. The attribution
table states each of those four facts separately, with the rows where the
filter does run standing as the controls for the rows where it does not, and
the same driver is declared a second time through ``include.path`` so that a
driver reachable only through an include is shown being found and neutralized.

Before constructing a hardened environment, boundver now injects a fixed probe
and requires Git to read it back at command scope. That is stronger than
trusting a version string: a Git that ignores the block, or a wrapper that
removes it, is refused before repository inspection continues. The tests still
inject ``core.filemode`` as a positive control showing that the mechanism can
move a working-tree digest, then prove removing the whole block prevents a
lockfile from being produced.

Covers OBL-GIT-SOURCE-153.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from unittest import mock

from boundver import _git as git
from boundver._utils import GuardrailError

from tests._parity import run_cli_in_process
from tests._scenarios import Scenario

#: The five process-local settings `_git_subprocess_env` injects for every
#: invocation, before it knows which repository is being inspected, at the
#: index each one occupies. `core.hooksPath` is compared against `os.devnull`
#: rather than a literal because it spells differently on Windows and POSIX.
FIXED_PROCESS_LOCAL_CONFIG: Tuple[Tuple[str, str], ...] = (
    ("core.hooksPath", os.devnull),
    ("core.fsmonitor", "false"),
    ("diff.ignoreSubmodules", "dirty"),
    ("status.submoduleSummary", "false"),
    ("submodule.recurse", "false"),
)

#: The four keys one declared filter driver must be neutralized by, and the
#: value each must carry. A driver left with any of them unset stays live.
NEUTRALIZED_FILTER: Dict[str, str] = {
    "filter.evil.clean": "",
    "filter.evil.smudge": "",
    "filter.evil.process": "",
    "filter.evil.required": "false",
}

#: How Git answers a `GIT_CONFIG_COUNT` it cannot use. The count and the
#: diagnostic are written as functions of the real count, because how many keys
#: the block carries depends on what the host's global config contributes.
#: A count of zero is the interesting row: Git disables the block and says
#: nothing, which is the same silence an older Git offers.
MALFORMED_COUNTS: Dict[str, Tuple[Callable[[int], str], int, Callable[[int], str]]] = {
    "a count that is not a number": (
        lambda count: "not-a-number",
        128,
        lambda count: (
            "error: bogus count in GIT_CONFIG_COUNT\n"
            "fatal: unable to parse command-line config\n"
        ),
    ),
    "a count one larger than the keys present": (
        lambda count: str(count + 1),
        128,
        lambda count: (
            f"error: missing config key GIT_CONFIG_KEY_{count}\n"
            "fatal: unable to parse command-line config\n"
        ),
    ),
    "a count of zero, which disables the block": (
        lambda count: "0",
        1,
        lambda count: "",
    ),
}

#: The path whose content the hostile fixture makes dirty, so that a Git
#: command comparing worktree against index has a reason to run a clean filter,
#: and the two spellings of its content.
#:
#: The two spellings are the same length on purpose. Git decides a tracked file
#: is modified from its stat data alone when the recorded size differs, and
#: never opens it, so a size-changing edit leaves whether the clean filter runs
#: a race between the edit and the index's own timestamp. With the size equal,
#: Git has to read the content through the filter to answer at all.
HOSTILE_FILE = "svc/a.txt"
HOSTILE_BEFORE = "hello\n"
HOSTILE_AFTER = "HELLO\n"

#: What `git status --porcelain` reports for the hostile fixture, under every
#: environment. The command must say the same thing either way; a Git that
#: discards the process-local block is silent, not loud.
HOSTILE_STATUS = f" M {HOSTILE_FILE}\n?? .gitattributes\n"

#: Git subcommands that compare a working-tree file against the index or write
#: one back, which is when Git consults `filter.<driver>.clean` or `.smudge`.
#: None of them is in boundver's offline allowlist, and the argv assertions
#: below say so rather than inferring it from a marker that stayed absent.
FILTER_INVOKING_SUBCOMMANDS = frozenset(
    {"add", "checkout", "commit", "restore", "stash", "status", "switch"}
)

SH = shutil.which("sh")

requires_posix_shell = unittest.skipUnless(
    SH is not None,
    "a Git clean filter is a shell command, so the fixture needs a POSIX sh",
)


def _repository() -> Scenario:
    """A committed one-component repository, declaring nothing hostile."""
    scene = Scenario()
    scene.component("svc", path="svc", provider="leaf")
    scene.file(HOSTILE_FILE, HOSTILE_BEFORE)
    scene.commit()
    return scene


def _declare_hostile_filter(scene: Scenario, script: Path) -> Scenario:
    """Point the repository's own config at an executable it controls.

    ``.gitattributes`` is written after the commit and left untracked. Git
    reads it from the worktree either way, and committing it would run the
    filter under the ambient environment before any measurement started.
    """
    command = f"sh '{script.as_posix()}'"
    scene.git("config", "filter.evil.clean", command)
    scene.git("config", "filter.evil.smudge", command)
    (scene.root / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8")
    scene.file(HOSTILE_FILE, HOSTILE_AFTER)
    return scene


def _declare_hostile_filter_through_an_include(
    scene: Scenario, script: Path
) -> Scenario:
    """Declare the same driver in a file the repository config includes.

    ``_repository_filter_config_overrides`` queries config names with
    ``--includes``, and its comment gives the reason: the Git command that
    follows would expand the same include, so a driver named only there is
    just as live as one named in ``.git/config`` directly. Nothing asserts
    that unless a driver arrives by this route, and the include is what makes
    the difference between ``--includes`` and ``--no-includes`` observable.
    """
    command = f"sh '{script.as_posix()}'"
    stanza = scene.root / ".git" / "filters.config"
    stanza.write_text(
        f'[filter "evil"]\n\tclean = {command}\n\tsmudge = {command}\n',
        encoding="utf-8",
    )
    # Relative to the directory holding the config that names it, so the
    # include stays inside .git and never becomes repository content.
    scene.git("config", "include.path", "filters.config")
    (scene.root / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8")
    scene.file(HOSTILE_FILE, HOSTILE_AFTER)
    return scene


def _without_process_local_config(mapping: Dict[str, str]) -> Dict[str, str]:
    """Drop the three variable families a Git older than 2.31 would ignore.

    Removing them models that Git faithfully in the respect that decides the
    outcome: Git does not complain about environment variables it does not
    recognise, so receiving them and ignoring them is indistinguishable from
    never having been sent them.

    ``GIT_CONFIG_GLOBAL`` and ``GIT_CONFIG_NOSYSTEM`` are deliberately left in
    place here, and that makes this narrower than a real pre-2.31 Git.
    ``GIT_CONFIG_NOSYSTEM`` predates all of this, but ``GIT_CONFIG_GLOBAL``
    arrived in 2.32, one release after ``GIT_CONFIG_COUNT``, so no real Git
    honours the second and ignores the first: a genuine 2.30 would also read
    the user's own global config. The narrowing runs in the direction that
    makes the divergence harder to prove rather than easier, since it denies
    the simulated old Git a source of settings that could have changed an
    answer. ``_pre_2_31_environment`` widens it to the faithful shape and
    measures what that costs.
    """
    stripped = dict(mapping)
    for name in tuple(stripped):
        canonical = name.upper()
        if canonical == "GIT_CONFIG_COUNT" or canonical.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            stripped.pop(name)
    return stripped


def _pre_2_31_environment(mapping: Dict[str, str], home: Path) -> Dict[str, str]:
    """Model Git 2.30 exactly: no process-local block, no ``GIT_CONFIG_GLOBAL``.

    2.30 does not know ``GIT_CONFIG_COUNT`` (2.31) or ``GIT_CONFIG_GLOBAL``
    (2.32) and ignores both in silence, while ``GIT_CONFIG_NOSYSTEM`` long
    predates them and is still honoured, so the system config stays suppressed
    and only the user's own global config comes back. *home* supplies that
    global config for the subprocess alone; nothing in ``os.environ`` moves.
    """
    stripped = _without_process_local_config(mapping)
    for name in tuple(stripped):
        if name.upper() == "GIT_CONFIG_GLOBAL":
            stripped.pop(name)
    stripped["HOME"] = str(home)
    stripped["USERPROFILE"] = str(home)
    for name in ("XDG_CONFIG_HOME", "HOMEDRIVE", "HOMEPATH"):
        stripped.pop(name, None)
    return stripped


def _injected(environment: Dict[str, str]) -> Dict[str, str]:
    """The process-local block, read out of the environment as Git would.

    An environment carrying no count carries no block, and answers with an
    empty mapping rather than raising, because the launch assertions below
    apply the same reading to every Git process a run started.
    """
    count = int(environment.get("GIT_CONFIG_COUNT", "0"))
    return {
        environment[f"GIT_CONFIG_KEY_{index}"]: environment[
            f"GIT_CONFIG_VALUE_{index}"
        ]
        for index in range(count)
    }


def _carrying_block(
    environment: Dict[str, str], pairs: Sequence[Tuple[str, str]]
) -> Dict[str, str]:
    """The same environment with *pairs* as its whole process-local block."""
    rebuilt = _without_process_local_config(environment)
    rebuilt["GIT_CONFIG_COUNT"] = str(len(pairs))
    for index, (key, value) in enumerate(pairs):
        rebuilt[f"GIT_CONFIG_KEY_{index}"] = key
        rebuilt[f"GIT_CONFIG_VALUE_{index}"] = value
    return rebuilt


def _without_injected_keys(
    environment: Dict[str, str], names: Iterable[str]
) -> Dict[str, str]:
    """Remove named keys from the block boundver built, renumbering the rest.

    Removing from the product's own block, rather than assembling a block by
    hand, keeps every partial row a statement about what boundver injects.
    """
    dropped = set(names)
    return _carrying_block(
        environment,
        [
            (key, value)
            for key, value in _injected(environment).items()
            if key not in dropped
        ],
    )


def _with_injected_keys(
    environment: Dict[str, str], pairs: Sequence[Tuple[str, str]]
) -> Dict[str, str]:
    """Append keys to the block boundver built, keeping the rest in order."""
    return _carrying_block(
        environment, list(_injected(environment).items()) + list(pairs)
    )


def _ambient_environment(root: Path) -> Dict[str, str]:
    return os.environ.copy()


def _hardened_environment(root: Path) -> Dict[str, str]:
    return git._git_subprocess_env(root)


def _discarded_environment(root: Path) -> Dict[str, str]:
    return _without_process_local_config(git._git_subprocess_env(root))


#: `git status --porcelain` against a repository whose own config declares a
#: clean filter, under each environment, and whether the filter is expected to
#: execute. The ambient row is the positive control and is listed first: with
#: no row that fires, "the filter did not run" would be a statement about the
#: fixture rather than about the process-local block.
FILTER_ENVIRONMENTS: Dict[str, Tuple[Callable[[Path], Dict[str, str]], bool]] = {
    "the ambient process environment": (_ambient_environment, True),
    "the environment _git_subprocess_env builds": (_hardened_environment, False),
    "that environment with the three variable families removed": (
        _discarded_environment,
        True,
    ),
}

#: Which of the four neutralizations the suppression actually rests on: the
#: keys removed from the block boundver built, and whether the driver then
#: runs. Measured, not reasoned about. Two keys stop the driver on their own,
#: so removing either alone changes nothing and only removing both lets it
#: back in; the rows where it runs are what keep the rows where it does not
#: from being a claim about a fixture that never worked.
PARTIAL_NEUTRALIZATIONS: Dict[str, Tuple[Tuple[str, ...], bool]] = {
    "the whole block boundver builds": ((), False),
    "clean alone, the other three removed": (
        ("filter.evil.smudge", "filter.evil.process", "filter.evil.required"),
        False,
    ),
    "process alone, the other three removed": (
        ("filter.evil.clean", "filter.evil.smudge", "filter.evil.required"),
        False,
    ),
    "smudge alone, the other three removed": (
        ("filter.evil.clean", "filter.evil.process", "filter.evil.required"),
        True,
    ),
    "required alone, the other three removed": (
        ("filter.evil.clean", "filter.evil.smudge", "filter.evil.process"),
        True,
    ),
    "everything except clean": (("filter.evil.clean",), False),
    "everything except process": (("filter.evil.process",), False),
    "everything except clean and process": (
        ("filter.evil.clean", "filter.evil.process"),
        True,
    ),
    "no filter neutralization at all": (tuple(NEUTRALIZED_FILTER), True),
}


def _subcommand(argv: Sequence[str]) -> Optional[str]:
    """The Git subcommand in a built argv, skipping every global option.

    ``_git_command`` puts ``-C <root>``, ``--work-tree=<root>``,
    ``--no-pager`` and a pair of ``-c`` assignments ahead of whatever
    ``_offline_git_command`` was asked for, so the subcommand is the first
    token that is neither an option nor an option's value.
    """
    index = 1
    while index < len(argv):
        token = argv[index]
        if token in {"-C", "-c"}:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return None


class _GitLaunch:
    """One Git process, as it was actually started: its argv and its env."""

    def __init__(self, argv: Sequence[str], environment: Dict[str, str]) -> None:
        self.argv = list(argv)
        self.environment = dict(environment)

    @property
    def subcommand(self) -> Optional[str]:
        return _subcommand(self.argv)

    @property
    def injected(self) -> Dict[str, str]:
        return _injected(self.environment)

    @property
    def is_bootstrap_query(self) -> bool:
        """Whether this config read establishes or builds the hardening block.

        Ambient-worktree, repository-filter, and partial-clone inspection use
        bounded ``config --get-regexp`` queries before a full block exists, so
        they cannot be held to the rule that every launch carries one.
        """
        return (
            "--get-regexp" in self.argv
            or git._PROCESS_CONFIG_PROBE_KEY in self.argv
            or self.subcommand == "version"
        )

    @property
    def is_process_config_probe(self) -> bool:
        return git._PROCESS_CONFIG_PROBE_KEY in self.argv

    @property
    def is_git_version_probe(self) -> bool:
        return self.subcommand == "version"

    @property
    def is_partial_clone_query(self) -> bool:
        return git._PARTIAL_CLONE_CONFIG_PATTERN in self.argv


@contextlib.contextmanager
def _recorded_git_launches(executable: str):
    """Record every Git process started, at the boundary where env is passed.

    Recording handouts from `_offline_git_environment` cannot see a launch
    that never asked it for an environment, which is the shape of the worst
    regression this obligation exists to catch. Patching `subprocess.Popen`
    for the duration of one in-process CLI run sees the argv and the mapping
    Git was really given, and the executable filter keeps a stray subprocess
    from some other library out of the measurement.
    """
    launches: List[_GitLaunch] = []
    real = subprocess.Popen

    def watched(command, *args, **kwargs):
        if (
            isinstance(command, (list, tuple))
            and command
            and command[0] == executable
        ):
            launches.append(_GitLaunch(command, kwargs.get("env") or {}))
        return real(command, *args, **kwargs)

    with mock.patch.object(subprocess, "Popen", watched):
        yield launches


class _EnvironmentRecorder:
    """A stand-in for `_offline_git_environment` that watches every handout."""

    def __init__(self, real, transform) -> None:
        self._real = real
        self._transform = transform
        self.handouts: List[Dict[str, str]] = []

    def __call__(self, repo_root=None, environment=None) -> Dict[str, str]:
        built = self._transform(self._real(repo_root, environment))
        self.handouts.append(dict(built))
        return built


@contextlib.contextmanager
def _simulated_git(*, strip: bool):
    """Patch the one function every Git subprocess takes its environment from.

    Patching `_git_subprocess_env` instead would miss the four sites that call
    `_offline_git_environment` directly and the caller-supplied environment
    path inside `_git_run`, and the simulation would then be of a Git that
    ignores the block only sometimes.
    """
    transform = _without_process_local_config if strip else dict
    recorder = _EnvironmentRecorder(git._offline_git_environment, transform)
    with mock.patch.object(git, "_offline_git_environment", recorder):
        yield recorder


@contextlib.contextmanager
def _simulated_git_injecting(pairs: Sequence[Tuple[str, str]]):
    """Append *pairs* to whatever block the package builds, at the choke point.

    This is the positive control for the fingerprint equality: it changes the
    block rather than removing it, so it shows the recorded document
    responding to the mechanism under test. A handout that carries no block
    is left alone, because appending to nothing would invent one.
    """

    def transform(built: Dict[str, str]) -> Dict[str, str]:
        if not built.get("GIT_CONFIG_COUNT"):
            return built
        return _with_injected_keys(built, pairs)

    recorder = _EnvironmentRecorder(git._offline_git_environment, transform)
    with mock.patch.object(git, "_offline_git_environment", recorder):
        yield recorder


@contextlib.contextmanager
def _simulated_git_2_30(home: Path):
    """The same patch, widened to the faithful shape ``_pre_2_31_environment``
    describes: the block gone and the user's global config back."""
    recorder = _EnvironmentRecorder(
        git._offline_git_environment,
        lambda built: _pre_2_31_environment(built, home),
    )
    with mock.patch.object(git, "_offline_git_environment", recorder):
        yield recorder


class _CommandRecorder:
    """A stand-in for `_offline_git_command` that keeps every argv built."""

    def __init__(self, real) -> None:
        self._real = real
        self.commands: List[List[str]] = []

    def __call__(self, repo_root, args) -> List[str]:
        command = self._real(repo_root, args)
        self.commands.append(list(command))
        return command


class _GitCaches:
    """Both lru_caches keyed on a repository root, cleared in both directions.

    Either one holding a tuple from a sibling test would silently supply the
    overrides a test here meant to observe being computed or removed.
    """

    def setUp(self):
        super().setUp()
        self._clear_git_caches()

    def tearDown(self):
        self._clear_git_caches()
        super().tearDown()

    @staticmethod
    def _clear_git_caches() -> None:
        git._repository_filter_config_overrides.cache_clear()
        git._ambient_worktree_config_overrides.cache_clear()


class _LaunchAssertions:
    """Statements about the Git processes one boundver run actually started."""

    def assert_every_launch_is_hardened(self, launches, resolved: str) -> None:
        """Every launch but the bootstrap queries carries the full block.

        The exceptions are named rather than skipped by count: the capability
        probe carries its own three-key block, the ambient
        promotion query reads system and global config on purpose, so it
        carries no block at all and passes the repository path as
        ``-c safe.directory=``; each partial-clone and filter query carries a
        three-key block including the exact repository path. Filter results are
        deliberately uncached, so a verify may issue more than one such query.
        """
        self.assertGreater(len(launches), 0, "the run started no Git process")
        bootstrap = [launch for launch in launches if launch.is_bootstrap_query]
        versions = [launch for launch in bootstrap if launch.is_git_version_probe]
        probes = [launch for launch in bootstrap if launch.is_process_config_probe]
        partial = [launch for launch in bootstrap if launch.is_partial_clone_query]
        promotion = [
            launch
            for launch in bootstrap
            if "--show-scope" in launch.argv and "--get-regexp" in launch.argv
        ]
        filters = [launch for launch in bootstrap if "--name-only" in launch.argv]
        self.assertEqual(len(versions), 1)
        self.assertEqual(len(probes), 1)
        self.assertEqual(len(partial), 1)
        self.assertEqual(len(promotion), 1)
        self.assertGreaterEqual(len(filters), 1)
        self.assertEqual(len(bootstrap), 4 + len(filters))
        self.assertEqual(versions[0].injected, {})
        self.assertEqual(
            probes[0].injected,
            {
                "core.hooksPath": os.devnull,
                "core.fsmonitor": "false",
                git._PROCESS_CONFIG_PROBE_KEY: git._PROCESS_CONFIG_PROBE_VALUE,
            },
        )
        self.assertEqual(promotion[0].injected, {})
        self.assertIn(f"safe.directory={resolved}", promotion[0].argv)
        self.assertEqual(
            partial[0].injected,
            {
                "core.hooksPath": os.devnull,
                "core.fsmonitor": "false",
                "safe.directory": resolved,
            },
        )
        for launch in filters:
            self.assertEqual(
                launch.injected,
                {
                    "core.hooksPath": os.devnull,
                    "core.fsmonitor": "false",
                    "safe.directory": resolved,
                },
            )

        hardened = [launch for launch in launches if not launch.is_bootstrap_query]
        self.assertGreaterEqual(len(hardened), 5)
        for launch in hardened:
            with self.subTest(subcommand=launch.subcommand):
                self.assertEqual(
                    launch.environment.get("GIT_CONFIG_GLOBAL"), os.devnull
                )
                self.assertEqual(
                    launch.environment.get("GIT_CONFIG_NOSYSTEM"), "1"
                )
                injected = launch.injected
                self.assertEqual(injected.get("safe.directory"), resolved)
                for key, value in FIXED_PROCESS_LOCAL_CONFIG:
                    self.assertEqual(injected.get(key), value)

    def assert_no_launch_carries_a_block(self, launches) -> None:
        self.assertGreater(len(launches), 0, "the run started no Git process")
        for launch in launches:
            with self.subTest(subcommand=launch.subcommand):
                self.assertNotIn("GIT_CONFIG_COUNT", launch.environment)
                leaked = [
                    name
                    for name in launch.environment
                    if name.upper().startswith(
                        ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
                    )
                ]
                self.assertEqual(leaked, [])

    def assert_the_verify_did_its_work(self, launches) -> None:
        """A floor with a meaning: the run read objects, not just config.

        Without it every universal statement above would hold of a verify
        that spawned one Git process and gave up.
        """
        subcommands = {launch.subcommand for launch in launches}
        self.assertLessEqual(
            {"rev-parse", "ls-tree", "cat-file", "config"}, subcommands
        )


class ProcessLocalConfigMechanismTests(_GitCaches, unittest.TestCase):
    """OBL-GIT-SOURCE-153: the installed Git applies the block boundver injects."""

    def test_the_block_carries_the_five_fixed_keys_and_then_the_repository_path(self):
        with _repository() as scene:
            environment = git._git_subprocess_env(scene.root)
            for index, (key, value) in enumerate(FIXED_PROCESS_LOCAL_CONFIG):
                with self.subTest(key=key):
                    self.assertEqual(environment[f"GIT_CONFIG_KEY_{index}"], key)
                    self.assertEqual(environment[f"GIT_CONFIG_VALUE_{index}"], value)
            self.assertEqual(environment["GIT_CONFIG_KEY_5"], "safe.directory")
            self.assertEqual(
                environment["GIT_CONFIG_VALUE_5"],
                str(scene.root.resolve(strict=False)),
            )
            count = int(environment["GIT_CONFIG_COUNT"])
            self.assertGreaterEqual(count, 6)
            self.assertEqual(len(_injected(environment)), count)

    def test_the_other_config_scopes_are_suppressed_alongside_it(self):
        """Which is what makes a read-back proof of anything at all.

        With system and global config still readable, a value returned by
        ``git config --get`` could have come from the host rather than from the
        block, and the whole mechanism test would prove nothing.
        """
        with _repository() as scene:
            environment = git._git_subprocess_env(scene.root)
            self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
            self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")

    def test_every_injected_key_reads_back_under_that_exact_environment(self):
        with _repository() as scene:
            environment = git._git_subprocess_env(scene.root)
            executable = git._trusted_git_executable(scene.root)
            injected = _injected(environment)
            self.assertIn("safe.directory", injected)
            for key, value in injected.items():
                with self.subTest(key=key):
                    result = subprocess.run(
                        [executable, "config", "--get", key],
                        cwd=scene.root,
                        env=environment,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.rstrip("\n"), value)

    def test_nothing_reads_back_once_the_three_variable_families_are_removed(self):
        """The control for the read-back: absent the block, no key resolves.

        ``safe.directory`` carries the resolved repository path, a value no
        other config scope on this host could be holding, so its disappearance
        is what identifies the block as the source.
        """
        with _repository() as scene:
            environment = git._git_subprocess_env(scene.root)
            executable = git._trusted_git_executable(scene.root)
            injected = _injected(environment)
            discarded = _without_process_local_config(environment)
            for key in injected:
                with self.subTest(key=key):
                    result = subprocess.run(
                        [executable, "config", "--get", key],
                        cwd=scene.root,
                        env=discarded,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr, "")

    def test_a_process_local_value_outranks_the_repositorys_own_declaration(self):
        """Precedence is the point: a repository must not be able to win."""
        with _repository() as scene:
            hooks = scene.root / "repository-hooks"
            hooks.mkdir()
            scene.git("config", "core.hooksPath", str(hooks))
            executable = git._trusted_git_executable(scene.root)
            self._clear_git_caches()
            hardened = git._git_subprocess_env(scene.root)
            under_block = subprocess.run(
                [executable, "config", "--get", "core.hooksPath"],
                cwd=scene.root,
                env=hardened,
                capture_output=True,
                text=True,
            )
            self.assertEqual(under_block.returncode, 0, under_block.stderr)
            self.assertEqual(under_block.stdout.rstrip("\n"), os.devnull)

            without_block = subprocess.run(
                [executable, "config", "--get", "core.hooksPath"],
                cwd=scene.root,
                env=_without_process_local_config(hardened),
                capture_output=True,
                text=True,
            )
            self.assertEqual(without_block.returncode, 0, without_block.stderr)
            self.assertEqual(without_block.stdout.rstrip("\n"), str(hooks))

    def test_git_refuses_a_malformed_count_rather_than_ignoring_the_block(self):
        """Proof that this Git parses the count instead of stepping over it.

        A Git that never looked at ``GIT_CONFIG_COUNT`` would answer all three
        rows identically, and the read-back tests above would then be reading
        values that arrived by some other route.
        """
        with _repository() as scene:
            environment = git._git_subprocess_env(scene.root)
            executable = git._trusted_git_executable(scene.root)
            count = int(environment["GIT_CONFIG_COUNT"])
            for label, (spelling, code, diagnostic) in MALFORMED_COUNTS.items():
                with self.subTest(count=label):
                    broken = dict(environment)
                    broken["GIT_CONFIG_COUNT"] = spelling(count)
                    result = subprocess.run(
                        [executable, "config", "--get", "safe.directory"],
                        cwd=scene.root,
                        env=broken,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, code)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr, diagnostic(count))


@requires_posix_shell
class HostileFilterSuppressionTests(
    _GitCaches, _LaunchAssertions, unittest.TestCase
):
    """OBL-GIT-SOURCE-153: what the injected block actually buys, measured."""

    def setUp(self):
        super().setUp()
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        # Both the script and its marker live outside the repository, so
        # neither becomes repository content the measurement would disturb.
        self.marker = Path(holder.name) / "filter-ran"
        self.script = Path(holder.name) / "evil.sh"
        self.script.write_text(
            "#!/bin/sh\n"
            f"printf 'ran\\n' >> '{self.marker.as_posix()}'\n"
            "cat\n",
            encoding="utf-8",
        )

    def _status(self, scene: Scenario, environment: Dict[str, str]):
        """Raw `git status --porcelain`, with the marker cleared first."""
        self.marker.unlink(missing_ok=True)
        return subprocess.run(
            [git._trusted_git_executable(scene.root), "status", "--porcelain"],
            cwd=scene.root,
            env=environment,
            capture_output=True,
            text=True,
        )

    def test_the_declared_filter_runs_under_the_ambient_environment(self):
        """The premise for every absence asserted in this class."""
        with _repository() as scene:
            _declare_hostile_filter(scene, self.script)
            result = self._status(scene, os.environ.copy())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(
                self.marker.exists(),
                "the fixture never executed its own filter, so nothing below "
                "would be measuring the process-local block",
            )

    def test_only_the_process_local_block_keeps_that_filter_from_running(self):
        """Every row gets its own repository, which is not tidiness.

        Git refreshes the index it reads, so a second command against the same
        repository can decline to re-run a filter it has already run, and the
        absence would then be an artifact of the previous measurement rather
        than of the environment under test.

        The row that fires and the row that does not differ by the whole
        block, so this measures the block entire; which key inside it does the
        suppressing is the attribution table's question, not this one's.
        """
        for label, (build, expected) in FILTER_ENVIRONMENTS.items():
            with self.subTest(environment=label):
                self._clear_git_caches()
                with _repository() as scene:
                    _declare_hostile_filter(scene, self.script)
                    result = self._status(scene, build(scene.root))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        result.stdout,
                        HOSTILE_STATUS,
                        "the command must report the same thing either way; a "
                        "Git that discards the block is silent, not loud",
                    )
                    self.assertEqual(self.marker.exists(), expected)

    def test_which_neutralization_the_suppression_actually_rests_on(self):
        """Per-key attribution, because the block is over-determined.

        Empty ``clean`` and empty ``process`` each stop the driver by
        themselves, so a behavioural row that only removes one of them sees
        nothing change, and a reader could conclude from the sibling test that
        every key in the block is load-bearing. Each row below removes a
        different subset of the four from the block boundver actually built
        and states what Git then does, and the rows that end with the filter
        running are the controls that keep the rest from being vacuous.
        """
        for label, (removed, expected) in PARTIAL_NEUTRALIZATIONS.items():
            with self.subTest(block=label):
                self._clear_git_caches()
                with _repository() as scene:
                    _declare_hostile_filter(scene, self.script)
                    hardened = git._git_subprocess_env(scene.root)
                    self.assertEqual(
                        set(NEUTRALIZED_FILTER) - set(_injected(hardened)),
                        set(),
                        "the fixture's driver was not neutralized at all, so "
                        "removing keys from the block measures nothing",
                    )
                    environment = _without_injected_keys(hardened, removed)
                    self.assertEqual(
                        set(removed) & set(_injected(environment)),
                        set(),
                        "the row did not actually remove what it names",
                    )
                    result = self._status(scene, environment)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, HOSTILE_STATUS)
                    self.assertEqual(self.marker.exists(), expected)

    def test_the_block_carries_all_four_neutralizations_for_that_driver(self):
        with _repository() as scene:
            _declare_hostile_filter(scene, self.script)
            self._clear_git_caches()
            environment = git._git_subprocess_env(scene.root)
            injected = _injected(environment)
            executable = git._trusted_git_executable(scene.root)
            for key, value in NEUTRALIZED_FILTER.items():
                with self.subTest(key=key):
                    self.assertEqual(injected.get(key), value)
                    result = subprocess.run(
                        [executable, "config", "--get", key],
                        cwd=scene.root,
                        env=environment,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.rstrip("\n"), value)

    def test_a_driver_reachable_only_through_an_include_is_still_neutralized(self):
        """The include is followed at discovery time, and it has to be.

        A driver named in a file the repository config includes is as live as
        one named directly, because the Git command that follows expands the
        same include. The ambient row is the positive control: it shows this
        second declaration route reaching the script at all, so the hardened
        row's silence is about the query following the include rather than
        about a stanza Git never read.
        """
        with _repository() as scene:
            _declare_hostile_filter_through_an_include(scene, self.script)
            ambient = self._status(scene, os.environ.copy())
            self.assertEqual(ambient.returncode, 0, ambient.stderr)
            self.assertEqual(ambient.stdout, HOSTILE_STATUS)
            self.assertTrue(self.marker.exists())

        self._clear_git_caches()
        with _repository() as scene:
            _declare_hostile_filter_through_an_include(scene, self.script)
            environment = git._git_subprocess_env(scene.root)
            self.assertEqual(
                {
                    key: value
                    for key, value in _injected(environment).items()
                    if key.startswith("filter.")
                },
                NEUTRALIZED_FILTER,
            )
            hardened = self._status(scene, environment)
            self.assertEqual(hardened.returncode, 0, hardened.stderr)
            self.assertEqual(hardened.stdout, HOSTILE_STATUS)
            self.assertFalse(self.marker.exists())

    def test_boundvers_own_commands_reach_the_filter_under_neither_environment(self):
        """The honest limit of the blast radius, pinned so it is not overstated.

        Losing the block does not let this repository's filter run during a
        completed boundver command, because the process-config capability gate
        refuses the operation first. That reason is asserted rather than
        inferred: every Git process each verify starts is recorded separately,
        none is one of the subcommands that compares a working tree against the
        index, and the two command-level refusals that make it so are provoked
        directly. The raw ambient control in the same test shows the fixture
        firing for a command boundver cannot issue.

        The working-tree verify exits 1 here, and that is the point of running
        it: the hostile edit is exactly what it reports, so it demonstrably
        read the modified file and still never handed it to a filter.
        """
        with _repository() as scene:
            self.assertEqual(run_cli_in_process(scene.root, "generate").returncode, 0)
            scene.git("add", "--all")
            scene.git("commit", "-m", "lockfile")
            head = scene.head()
            _declare_hostile_filter(scene, self.script)
            executable = git._trusted_git_executable(scene.root)
            resolved = str(scene.root.resolve(strict=False))

            control = self._status(scene, os.environ.copy())
            self.assertEqual(control.returncode, 0, control.stderr)
            self.assertTrue(self.marker.exists())

            with self.assertRaises(ValueError) as refused_status:
                git._offline_git_command(scene.root, ["status", "--porcelain"])
            self.assertIn("offline allowlist", str(refused_status.exception))
            with self.assertRaises(ValueError) as refused_diff:
                git._offline_git_command(scene.root, ["diff"])
            self.assertIn("clean filters", str(refused_diff.exception))

            for label, strip in (
                ("the process-local block intact", False),
                ("the process-local block discarded", True),
            ):
                with self.subTest(mechanism=label):
                    self.marker.unlink(missing_ok=True)
                    for source, args in (
                        ("head", ()),
                        ("working-tree", ("--source", "working-tree")),
                    ):
                        with self.subTest(mechanism=label, source=source):
                            self._clear_git_caches()
                            with _simulated_git(strip=strip):
                                with _recorded_git_launches(executable) as launches:
                                    result = run_cli_in_process(
                                        scene.root, "verify", *args
                                    )

                            subcommands = {
                                launch.subcommand for launch in launches
                            }
                            self.assertEqual(
                                subcommands & FILTER_INVOKING_SUBCOMMANDS, set()
                            )
                            self.assertLessEqual(
                                subcommands, git._OFFLINE_GIT_SUBCOMMANDS
                            )
                            if strip:
                                self.assertEqual(result.returncode, 2)
                                self.assertEqual(result.stdout, "")
                                self.assertIn(
                                    "did not apply process-local security",
                                    result.stderr,
                                )
                                self.assert_no_launch_carries_a_block(launches)
                            else:
                                if source == "head":
                                    self.assertEqual(
                                        result.returncode, 0, result.stderr
                                    )
                                    self.assertIn(
                                        "Lockfile is up to date.", result.stdout
                                    )
                                    self.assertIn(f"HEAD@{head}", result.stdout)
                                else:
                                    self.assertEqual(
                                        result.returncode, 1, result.stderr
                                    )
                                    self.assertIn(
                                        "MISMATCH svc.exact", result.stdout
                                    )
                                if source == "head":
                                    self.assert_the_verify_did_its_work(launches)
                                else:
                                    self.assertLessEqual(
                                        {
                                            "rev-parse",
                                            "ls-files",
                                            "write-tree",
                                            "config",
                                        },
                                        subcommands,
                                    )
                                self.assert_every_launch_is_hardened(
                                    launches, resolved
                                )
                            self.assertFalse(self.marker.exists())


class GitCapabilityGateTests(unittest.TestCase):
    """The operation gate binds a supported version to a live read-back."""

    def test_the_installed_version_parser_preserves_distribution_suffixes(self):
        response = subprocess.CompletedProcess(
            ["git", "version"], 0, "git version 2.55.0.windows.3\n", ""
        )
        with mock.patch.object(git, "_git_run", return_value=response) as runner:
            parsed, display = git._installed_git_version("repo")
        self.assertEqual(parsed, (2, 55, 0))
        self.assertEqual(display, "2.55.0.windows.3")
        self.assertEqual(runner.call_args.args[1], ["version"])

    def test_a_version_below_the_security_floor_is_named_and_refused(self):
        response = subprocess.CompletedProcess(
            ["git", "version"], 0, "git version 2.31.9\n", ""
        )
        with mock.patch.object(git, "_git_run", return_value=response) as runner:
            with self.assertRaises(GuardrailError) as raised:
                git._require_process_local_git_config("repo")
        self.assertEqual(runner.call_count, 1)
        self.assertIn("Git 2.31.9", str(raised.exception))
        self.assertIn("required Git 2.32.0", str(raised.exception))

    def test_malformed_version_output_fails_closed(self):
        response = subprocess.CompletedProcess(
            ["git", "version"], 0, "Git from somewhere\n", ""
        )
        with mock.patch.object(git, "_git_run", return_value=response):
            with self.assertRaisesRegex(
                GuardrailError, "Cannot determine the installed Git version"
            ):
                git._require_process_local_git_config("repo")

    def test_a_supported_version_must_still_echo_the_command_scope_probe(self):
        responses = [
            subprocess.CompletedProcess(
                ["git", "version"], 0, "git version 2.55.0.windows.3\n", ""
            ),
            subprocess.CompletedProcess(["git", "config"], 0, "local\0wrong\0", ""),
        ]
        with mock.patch.object(git, "_git_run", side_effect=responses):
            with self.assertRaises(GuardrailError) as raised:
                git._require_process_local_git_config("repo")
        detail = str(raised.exception)
        self.assertIn("Git 2.55.0.windows.3", detail)
        self.assertIn("minimum Git 2.32.0", detail)


class ReadBackAndFailClosedTests(_GitCaches, _LaunchAssertions, unittest.TestCase):
    """OBL-GIT-SOURCE-153: a Git that ignores the block must not finish a verify."""

    def _committed_lockfile(self, scene: Scenario) -> str:
        self.assertEqual(run_cli_in_process(scene.root, "generate").returncode, 0)
        scene.git("add", "--all")
        scene.git("commit", "-m", "lockfile")
        return scene.head()

    def test_the_simulation_removes_the_block_from_every_git_launch(self):
        """Premise: without this the divergence below could be a no-op patch.

        The unpatched run is the control, and what it establishes is a
        property of every Git process the verify started rather than of the
        function that built an environment: each one but the two named
        bootstrap query kinds received a block carrying this repository's resolved
        path and the five fixed keys, with system and global config suppressed
        beside them. A weaker control that only asked whether the name
        ``GIT_CONFIG_COUNT`` appeared somewhere would accept a package that
        had stopped passing the repository root, or that had stopped routing
        some of its launches through the choke point at all, and the patched
        run's silence would then be measuring nothing.
        """
        with _repository() as scene:
            self._committed_lockfile(scene)
            executable = git._trusted_git_executable(scene.root)
            resolved = str(scene.root.resolve(strict=False))

            self._clear_git_caches()
            with _simulated_git(strip=False):
                with _recorded_git_launches(executable) as intact:
                    self.assertEqual(
                        run_cli_in_process(scene.root, "verify").returncode, 0
                    )
            self.assert_the_verify_did_its_work(intact)
            self.assert_every_launch_is_hardened(intact, resolved)

            self._clear_git_caches()
            with _simulated_git(strip=True):
                with _recorded_git_launches(executable) as stripped:
                    result = run_cli_in_process(scene.root, "verify")
            self.assertEqual(result.returncode, 2)
            self.assertIn("did not apply process-local security", result.stderr)
            self.assert_no_launch_carries_a_block(stripped)
            self.assertEqual(
                sum(launch.is_process_config_probe for launch in stripped), 1
            )

    def test_a_git_that_ignores_the_block_cannot_complete_a_verify(self):
        """Exit 2 is used because docs/security-model.md already
        reserves it for input that cannot be trusted for an authoritative
        comparison, which is what a toolchain of unknown hardening is.
        """
        with _repository() as scene:
            self._committed_lockfile(scene)
            self._clear_git_caches()
            with _simulated_git(strip=True):
                result = run_cli_in_process(scene.root, "verify")
            self.assertEqual(result.returncode, 2)
            self.assertNotEqual(result.stderr, "")

    def test_verify_does_not_report_success_when_the_block_is_discarded(self):
        with _repository() as scene:
            self._committed_lockfile(scene)
            self._clear_git_caches()
            with _simulated_git(strip=True):
                result = run_cli_in_process(scene.root, "verify")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertIn("did not apply process-local security", result.stderr)

    def test_the_working_tree_source_fails_closed_the_same_way(self):
        with _repository() as scene:
            self._committed_lockfile(scene)
            self._clear_git_caches()
            with _simulated_git(strip=True):
                result = run_cli_in_process(
                    scene.root, "verify", "--source", "working-tree"
                )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertIn("did not apply process-local security", result.stderr)

    def test_a_faithful_pre_2_31_git_is_refused(self):
        """The simulation widened to the shape a real Git 2.30 would have.

        Everywhere else in this file the simulated old Git keeps honouring
        ``GIT_CONFIG_GLOBAL``, which no Git that ignores ``GIT_CONFIG_COUNT``
        could do, so the user's own global config never comes back. Here it
        does, out of a planted home the subprocess alone sees, and the control
        proves the widening took: under this environment the planted global
        ``core.hooksPath`` is what Git reports, while under the block it is
        still the null device. The verify then fails exactly as the narrower
        simulation does, proving the refusal is about process-local
        config support rather than the simulation's convenient blind spot.
        """
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        home = Path(holder.name)
        planted = home / "planted-hooks"
        (home / ".gitconfig").write_text(
            f"[core]\n\thooksPath = {planted.as_posix()}\n"
            "[boundver]\n\tplanted = yes\n",
            encoding="utf-8",
        )

        with _repository() as scene:
            self._committed_lockfile(scene)
            executable = git._trusted_git_executable(scene.root)
            self._clear_git_caches()
            hardened = git._git_subprocess_env(scene.root)
            widened = _pre_2_31_environment(hardened, home)

            def read(key: str, environment: Dict[str, str]):
                return subprocess.run(
                    [executable, "config", "--get", key],
                    cwd=scene.root,
                    env=environment,
                    capture_output=True,
                    text=True,
                )

            under_block = read("core.hooksPath", hardened)
            self.assertEqual(under_block.returncode, 0, under_block.stderr)
            self.assertEqual(under_block.stdout.rstrip("\n"), os.devnull)
            self.assertEqual(read("boundver.planted", hardened).returncode, 1)

            restored = read("core.hooksPath", widened)
            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.assertEqual(restored.stdout.rstrip("\n"), planted.as_posix())
            probe = read("boundver.planted", widened)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertEqual(probe.stdout.rstrip("\n"), "yes")

            self._clear_git_caches()
            with _simulated_git_2_30(home) as recorder:
                result = run_cli_in_process(scene.root, "verify")
            self.assertGreater(len(recorder.handouts), 0)
            suppressing_the_system = 0
            for handout in recorder.handouts:
                self.assertNotIn("GIT_CONFIG_COUNT", handout)
                self.assertNotIn("GIT_CONFIG_GLOBAL", handout)
                self.assertEqual(handout["HOME"], str(home))
                # The capability probe is the only handout reached before the
                # fail-closed result, and it keeps the system suppression a
                # 2.30 would still honour.
                if "GIT_CONFIG_NOSYSTEM" in handout:
                    self.assertEqual(handout["GIT_CONFIG_NOSYSTEM"], "1")
                    suppressing_the_system += 1
            self.assertGreater(suppressing_the_system, 0)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertIn("did not apply process-local security", result.stderr)

    def test_generate_refuses_a_git_that_discards_the_block(self):
        """No document is produced under an unverified toolchain.

        An equality between two runs proves nothing unless the document could
        have moved, so the middle arm moves it: ``core.filemode`` appended to
        the block boundver built is set to the opposite of the fixture's real
        repository value. That changes the mode a working-tree sample records
        for a file the index carries as 100755, on both Windows and POSIX, and
        the lockfile comes out different. The hostile filter is not the lever
        here and could not be, because boundver reads blobs with ``cat-file``
        and working-tree content itself, so no digest input ever passes through
        a clean filter. What the block can move is what it tells Git about the
        worktree. The third arm proves removing the block is detected before
        output exists.
        """
        with _repository() as scene:
            lockfile = scene.root / "boundary.lock.json"
            scene.git("update-index", "--chmod=+x", HOSTILE_FILE)
            scene.git("commit", "-m", "executable bit")
            self.assertIn("100755", scene.git("ls-files", "-s", HOSTILE_FILE))

            self._clear_git_caches()
            self.assertEqual(
                run_cli_in_process(
                    scene.root, "generate", "--source", "working-tree"
                ).returncode,
                0,
            )
            intact = lockfile.read_bytes()

            lockfile.unlink()
            self._clear_git_caches()
            configured_filemode = scene.git(
                "config", "--bool", "core.filemode"
            ).strip()
            self.assertIn(configured_filemode, {"true", "false"})
            opposite_filemode = (
                "false" if configured_filemode == "true" else "true"
            )
            with _simulated_git_injecting(
                (("core.filemode", opposite_filemode),)
            ):
                self.assertEqual(
                    run_cli_in_process(
                        scene.root, "generate", "--source", "working-tree"
                    ).returncode,
                    0,
                )
            moved = lockfile.read_bytes()
            self.assertNotEqual(
                moved,
                intact,
                "the recorded document is insensitive to the block, so the "
                "equality below would hold whatever the simulation did",
            )

            lockfile.unlink()
            self._clear_git_caches()
            with _simulated_git(strip=True):
                result = run_cli_in_process(
                    scene.root, "generate", "--source", "working-tree"
                )
            self.assertEqual(result.returncode, 2)
            self.assertFalse(lockfile.exists())

    def test_only_the_capability_probe_reads_an_injected_key_back(self):
        """The probe is the sole deliberate read-back of process-local config.

        Recording argv rather than reading the source keeps this a statement
        about what runs. Membership is tested by substring rather than by
        whole-element equality, so a read-back spelled ``-c core.hooksPath=``
        or ``--get-regexp ^safe\\.directory$`` would be caught as readily as
        ``config --get``, and the two argv elements that legitimately name an
        injected key are named here instead of being quietly permitted:
        ``_offline_git_command`` repeats ``core.fsmonitor=false`` on every
        command line, and the ambient promotion query carries the repository
        path as ``safe.directory=<root>`` because it runs before a block
        exists. The assertion that some argv was recorded at all is what stops
        this from being a claim about a verify that never spawned Git.
        """
        with _repository() as scene:
            self._committed_lockfile(scene)
            self._clear_git_caches()
            injected = _injected(git._git_subprocess_env(scene.root))
            resolved = str(scene.root.resolve(strict=False))
            permitted = {"core.fsmonitor=false", f"safe.directory={resolved}"}
            recorder = _CommandRecorder(git._offline_git_command)
            self._clear_git_caches()
            with mock.patch.object(git, "_offline_git_command", recorder):
                self.assertEqual(
                    run_cli_in_process(scene.root, "verify").returncode, 0
                )
            self.assertGreater(len(recorder.commands), 0)
            probes = [
                command
                for command in recorder.commands
                if git._PROCESS_CONFIG_PROBE_KEY in command
            ]
            self.assertEqual(len(probes), 1)
            mentions = set()
            for command in recorder.commands:
                self.assertNotIn("--version", command)
                if command not in probes:
                    self.assertNotIn("--get", command)
                for element in command[1:]:
                    for key in injected:
                        if key in element:
                            mentions.add(element)
            self.assertEqual(mentions, permitted)
