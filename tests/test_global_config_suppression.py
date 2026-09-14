"""A global Git configuration is executable input that boundver never chose.

Git reads ``$HOME/.gitconfig`` before it reads anything the repository says,
and that file can name a hooks directory, bind a filter driver to a shell
script, pull in a second file through ``include.path``, and open a trace2 sink
that records every argv and repository path Git touches. Anyone who can write
to a developer's home directory, and any CI image that ships an opinionated
one, therefore sits between boundver and the bytes it was asked to digest. If
any of that reaches the Git that boundver runs, a lockfile stops being a
statement about the repository and becomes a statement about the home
directory.

The hard part of testing this is that a passing run looks exactly like a run
where nothing happened: two clean arms compare byte-identical too. The
comparison proves nothing until the fixture can show that the hostile
configuration is genuinely in front of Git and genuinely able to fire. Most of
this file is therefore premise. Plain Git under the same home resolves each
planted setting at global scope and finds nothing under a clean one; plain Git
under that home writes the three trace files and runs the planted hook and
filter; and boundver's own ambient-config reader, which deliberately copies a
narrow worktree allowlist out of global scope, can see the file. Only then are
thirteen boundver invocations compared across a clean home, the hostile home,
and the clean home again.

The fixture also had to plant damage a byte comparison cannot see. A build that
leaked only the trace sinks would write the same lockfile with the same digests
and still hand the attacker an event log naming every path it read, so the
trace files and the hook and filter markers are asserted apart from the output,
and each of those assertions is preceded by a control showing that same
directory filling up.

Those absence assertions are not all equally sharp, and the file now measures
how sharp each one is rather than leaving a reader to assume. Removing the
global-config redirect alone leaks no trace file, because the trace2 pins still
win; removing the trace2 pins alone leaks none either, because the file naming
the sinks is unreadable; only both together produce the three files, and
``test_two_faults_are_needed_before_a_trace_file_appears`` runs all four
environments and requires exactly that. The hook and filter markers are blunter
still: none of the fourteen allowlisted subcommands asks Git to filter a file
or refresh the index, so the markers stay absent even with the redirect gone,
which ``test_no_allowlisted_subcommand_leaks_what_git_status_leaks`` shows by
running the weakened environment through boundver's own subcommands and then
through ``git status``, which alone fires the clean filter. The load-bearing
single-fault detectors are the environment table and the read-back in
``ProcessLocalReadBackTests``, and they are named as such.

One narrow channel is deliberately left open. boundver copies six inert
worktree settings out of system and global scope into process-local config so a
hardened Git still sees line-ending, symlink, case, and Unicode conventions.
``core.filemode`` is intentionally excluded: unlike those six settings, an
ambient false value can move working-tree ``exact`` and ``behavior`` digests.
Repository and worktree declarations remain visible, but a ``~/.gitconfig``
cannot decide a repository fingerprint.

What this file cannot establish is the obligation's last clause: one Git is
installed here, so every result below is about that one, and the class that
would check a second is skipped unless a second is named.

Covers OBL-GIT-SOURCE-154.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple
from unittest import mock

from boundver import _git as git

from tests._parity import run_cli
from tests._repo_fixtures import init_git_repo
from tests._scenarios import Scenario

#: Every setting the fixture plants in the hostile home, mapped to the file
#: that declares it. "root" is ~/.gitconfig, which Git reads directly;
#: "included" is the second file reached only through include.path, which is
#: how the obligation asks for the include clause to be exercised.
PLANTED = {
    "core.hooksPath": "root",
    "filter.evil.clean": "root",
    "filter.evil.smudge": "root",
    "core.eol": "root",
    "core.filemode": "root",
    "trace2.eventTarget": "root",
    "trace2.normalTarget": "root",
    "trace2.perfTarget": "root",
    "core.attributesFile": "included",
    "core.fsmonitor": "included",
    "core.safecrlf": "included",
    "filter.evil2.clean": "included",
}

#: The boundver surface compared between homes. Every one of these exits 0 on
#: the fixture repository; a variant that failed identically in both arms would
#: compare equal while proving nothing, which is why that is asserted first.
COMMANDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("generate-head", ("generate", "--source", "head")),
    ("generate-index", ("generate", "--source", "index")),
    ("generate-working-tree", ("generate", "--source", "working-tree")),
    ("verify-head", ("verify",)),
    ("verify-working-tree", ("verify", "--source", "working-tree")),
    ("verify-json", ("verify", "--source", "working-tree", "--format", "json")),
    ("status-head", ("status",)),
    ("status-working-tree", ("status", "--source", "working-tree")),
    ("explain", ("explain", "svc")),
    ("review", ("review", "HEAD~1..HEAD")),
    ("review-json", ("review", "HEAD~1..HEAD", "--format", "json")),
    ("validate-config", ("validate-config",)),
    ("discover", ("discover",)),
)

#: What the installed Git must resolve for each planted key once boundver's
#: process-local block is in force. None means the key must not resolve at all.
SUPPRESSED = {
    "core.hooksPath": os.devnull,
    "core.fsmonitor": "false",
    "filter.evil.clean": None,
    "filter.evil.smudge": None,
    "filter.evil2.clean": None,
    "trace2.eventTarget": None,
    "trace2.normalTarget": None,
    "trace2.perfTarget": None,
    "core.attributesFile": None,
}

#: Environment names boundver's Git environment must pin, and to what.
PINNED_ENVIRONMENT = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_TRACE2": "0",
    "GIT_TRACE2_EVENT": "0",
    "GIT_TRACE2_PERF": "0",
    "GIT_CONFIG_KEY_0": "core.hooksPath",
    "GIT_CONFIG_VALUE_0": os.devnull,
    "GIT_CONFIG_KEY_1": "core.fsmonitor",
    "GIT_CONFIG_VALUE_1": "false",
}

#: The three files the planted trace2 sinks write, in the order side_effects
#: reports them. Named once so no test has to spell them twice.
TRACE_FILES = ["trace2-event.json", "trace2-normal.log", "trace2-perf.log"]

#: Environments derived from the real one by deleting one family of names, and
#: what plain Git then leaves behind. This is the shape of the headline
#: side-effect assertion: neither single deletion leaks anything, so the empty
#: marker directory is a two-fault detector rather than a pin on either name.
TRACE_WEAKENINGS: Tuple[Tuple[str, Tuple[str, ...], List[str]], ...] = (
    ("nothing", (), []),
    ("the global config redirect", ("GIT_CONFIG_GLOBAL",), []),
    ("the trace2 pins", ("GIT_TRACE2", "GIT_TRACE2_EVENT", "GIT_TRACE2_PERF"), []),
    (
        "both",
        (
            "GIT_CONFIG_GLOBAL",
            "GIT_TRACE2",
            "GIT_TRACE2_EVENT",
            "GIT_TRACE2_PERF",
        ),
        TRACE_FILES,
    ),
)

#: Plumbing calls drawn from boundver's offline subcommand allowlist, chosen
#: because each reads the index or the working tree and so is where a leaked
#: clean filter or fsmonitor hook would fire if any of them could reach one.
#: `git status`, which does fire them, is not in that allowlist and is the
#: contrast the reachability test runs afterwards.
ALLOWLISTED_PROBES: Tuple[Tuple[str, ...], ...] = (
    ("rev-parse", "HEAD"),
    ("ls-files", "--stage"),
    ("diff", "--cached", "--name-only"),
    ("ls-tree", "-r", "HEAD"),
    ("cat-file", "-p", "HEAD:boundary.config.json"),
    ("write-tree",),
)

#: Ambient names planted in os.environ to prove the stripping loop runs. Each
#: would redirect or instrument Git if it survived into the subprocess.
AMBIENT_POISON = {
    "GIT_CONFIG_GLOBAL": "should-be-replaced",
    "GIT_DIR": "should-not-survive",
    "GIT_EXTERNAL_DIFF": "should-not-survive",
    "GIT_TRACE2_EVENT": "should-be-replaced",
    "SSH_ASKPASS": "should-not-survive",
}

_SHELL = shutil.which("sh")
requires_posix_shell = unittest.skipUnless(
    _SHELL is not None,
    "the planted hook and filter are /bin/sh scripts, and no sh is on PATH",
)

#: A second Git to repeat the read-back against. The obligation asks for every
#: Git in the supported range; this host has one, so a CI leg that installs
#: another points this at it rather than the assertion being dropped.
ALTERNATE_GIT = os.environ.get("BOUNDVER_ALTERNATE_GIT")


class HostileHome:
    """A home directory whose global Git config is an attack."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.hooks = root / "evilhooks"
        self.markers = root / "markers"
        self.config = root / ".gitconfig"
        self.included = root / "included.gitconfig"
        self.attributes = root / "evil-attributes"
        self.clean_script = root / "evil-clean.sh"
        self.smudge_script = root / "evil-smudge.sh"
        self.fsmonitor_script = root / "evil-fsmonitor.sh"
        self.traces = {
            "trace2.eventTarget": root / "trace2-event.json",
            "trace2.normalTarget": root / "trace2-normal.log",
            "trace2.perfTarget": root / "trace2-perf.log",
        }
        self._build()

    # ---- construction -----------------------------------------------------

    def _script(self, path: Path, body: str) -> None:
        """Write an executable /bin/sh script with real line feeds."""
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8", newline="\n")
        os.chmod(path, 0o700)

    def _build(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.hooks.mkdir()
        self.markers.mkdir()
        recorded = self.markers.as_posix()
        self._script(
            self.clean_script,
            'printf "clean\\n" >> "' + recorded + '/filter-clean"\n'
            'printf "PWNED-BY-GLOBAL-FILTER\\n"\n'
            "cat > /dev/null\n",
        )
        self._script(
            self.smudge_script,
            'printf "smudge\\n" >> "' + recorded + '/filter-smudge"\n'
            "cat\n",
        )
        self._script(
            self.fsmonitor_script,
            'printf "fsmonitor\\n" >> "' + recorded + '/fsmonitor"\n'
            "exit 1\n",
        )
        for hook in (
            "pre-commit",
            "post-index-change",
            "post-checkout",
            "reference-transaction",
            "pre-auto-gc",
        ):
            self._script(
                self.hooks / hook,
                'printf "%s\\n" "' + hook + '" >> "' + recorded + '/hooks"\n'
                "exit 0\n",
            )
        self.attributes.write_text(
            "* filter=evil\n* text=auto\n", encoding="utf-8", newline="\n"
        )
        # Reached only through include.path, so anything promoted out of here
        # would prove the --no-includes flag on boundver's config query is not
        # holding.
        self.included.write_text(
            "[core]\n"
            "\tsafecrlf = warn\n"
            "\tattributesFile = " + self.attributes.as_posix() + "\n"
            "\tfsmonitor = " + self.fsmonitor_script.as_posix() + "\n"
            '[filter "evil2"]\n'
            "\tclean = " + self.clean_script.as_posix() + "\n"
            "\trequired = false\n"
            "[safe]\n"
            "\tdirectory = *\n",
            encoding="utf-8",
            newline="\n",
        )
        self.config.write_text(
            "[user]\n"
            "\tname = Hostile Global\n"
            "\temail = hostile@example.invalid\n"
            "[core]\n"
            "\thooksPath = " + self.hooks.as_posix() + "\n"
            "\teol = crlf\n"
            # The one promotable key that can move a digest. Everything else
            # here is suppressed outright; this one is copied into
            # process-local config on purpose, so it is the channel a hostile
            # home still reaches. `false` rather than `true` because an unset
            # value already means true, and an inert plant proves nothing.
            "\tfilemode = false\n"
            '[filter "evil"]\n'
            "\tclean = " + self.clean_script.as_posix() + "\n"
            "\tsmudge = " + self.smudge_script.as_posix() + "\n"
            "\trequired = false\n"
            "[include]\n"
            "\tpath = " + self.included.as_posix() + "\n"
            "[trace2]\n"
            "\teventTarget = " + self.traces["trace2.eventTarget"].as_posix() + "\n"
            "\tnormalTarget = " + self.traces["trace2.normalTarget"].as_posix() + "\n"
            "\tperfTarget = " + self.traces["trace2.perfTarget"].as_posix() + "\n",
            encoding="utf-8",
            newline="\n",
        )
        # XDG is the second route to a global config on POSIX, so it is hostile
        # too and a test may point XDG_CONFIG_HOME here safely.
        xdg = self.root / ".config" / "git"
        xdg.mkdir(parents=True)
        shutil.copyfile(self.config, xdg / "config")
        (xdg / "attributes").write_text(
            "* filter=evil\n", encoding="utf-8", newline="\n"
        )

    # ---- what the fixture declares ----------------------------------------

    def declared(self, key: str) -> Tuple[Path, str]:
        """The file that declares *key* and the exact value Git must report."""
        values = {
            "core.hooksPath": (self.config, self.hooks.as_posix()),
            "filter.evil.clean": (self.config, self.clean_script.as_posix()),
            "filter.evil.smudge": (self.config, self.smudge_script.as_posix()),
            "core.eol": (self.config, "crlf"),
            "core.filemode": (self.config, "false"),
            "trace2.eventTarget": (
                self.config,
                self.traces["trace2.eventTarget"].as_posix(),
            ),
            "trace2.normalTarget": (
                self.config,
                self.traces["trace2.normalTarget"].as_posix(),
            ),
            "trace2.perfTarget": (
                self.config,
                self.traces["trace2.perfTarget"].as_posix(),
            ),
            "core.attributesFile": (self.included, self.attributes.as_posix()),
            "core.fsmonitor": (self.included, self.fsmonitor_script.as_posix()),
            "core.safecrlf": (self.included, "warn"),
            "filter.evil2.clean": (self.included, self.clean_script.as_posix()),
        }
        return values[key]

    # ---- what running Git left behind -------------------------------------

    def side_effects(self) -> List[str]:
        """Marker and trace files, relative to the home, in a stable order."""
        found = []
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(self.root).as_posix()
            if relative.startswith("markers/") or "trace2-" in relative:
                found.append(relative)
        return sorted(found)

    def clear_side_effects(self) -> None:
        for relative in self.side_effects():
            (self.root / relative).unlink()


def clean_home(root: Path) -> Path:
    """A home holding no Git configuration at all, on either route."""
    (root / ".config" / "git").mkdir(parents=True)
    return root


def narrow_home(root: Path, core_body: str) -> Path:
    """A home whose global config declares one `[core]` key and nothing else.

    The full hostile fixture plants eleven settings at once, which is right for
    asking whether any of them survives but wrong for asking which one moved a
    digest. These single-key homes answer the second question directly.
    """
    (root / ".config" / "git").mkdir(parents=True)
    (root / ".gitconfig").write_text(
        "[user]\n"
        "\tname = Narrow Global\n"
        "\temail = narrow@example.invalid\n"
        "[core]\n" + core_body,
        encoding="utf-8",
        newline="\n",
    )
    return root


@contextmanager
def home_pointed_at(home: Path) -> Iterator[None]:
    """Redirect every route Git takes to a home directory, then restore.

    Git resolves a home from HOME, then HOMEDRIVE plus HOMEPATH, then
    USERPROFILE on Windows, and reads XDG_CONFIG_HOME on every platform. An
    inherited HOMESHARE outranks some of those, so it is removed rather than
    replaced. ``patch.dict`` restores the mapping wholesale on exit, which puts
    HOMESHARE back for whatever runs next in this process.
    """
    drive, tail = os.path.splitdrive(str(home))
    replacement = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "HOMEDRIVE": drive,
        "HOMEPATH": tail,
        "XDG_CONFIG_HOME": str(home / ".config"),
    }
    with mock.patch.dict(os.environ, replacement, clear=False):
        os.environ.pop("HOMESHARE", None)
        yield


def plain_git(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Git as the ambient environment configures it: nothing suppressed."""
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True
    )


def resolved_setting(root: Path, key: str) -> subprocess.CompletedProcess:
    """Ask plain Git which scope and file a single setting comes from."""
    return plain_git(root, "config", "--show-scope", "--show-origin", "--get", key)


def drop_local_filemode(root: Path) -> None:
    """Remove the ``core.filemode`` that ``git init`` writes into every repository.

    Git probes the filesystem at init time and records the answer locally, on
    Windows and on POSIX alike; this host writes ``filemode = false``. A local
    value outranks a global one, so while it is there the global plant is
    masked and nothing measures the promotion channel. Removing it is what
    makes the hostile home's ``core.filemode`` the effective value, which is
    also the shape of any repository whose ``.git/config`` was templated or
    hand-edited without the key.
    """
    result = plain_git(root, "config", "--unset", "core.filemode")
    # 5 is "you tried to unset a key that is not set", which is fine here.
    assert result.returncode in {0, 5}, result.stderr
    check = plain_git(root, "config", "--local", "--get", "core.filemode")
    assert check.returncode == 1, check.stdout


def strip_process_local(environment: Dict[str, str]) -> Dict[str, str]:
    """The same environment with the process-local config block removed.

    This is the control for every read-back: without it, a key resolving to a
    harmless value proves only that the repository never declared a harmful
    one.
    """
    stripped = dict(environment)
    for name in tuple(stripped):
        canonical = name.upper()
        if canonical.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")) or (
            canonical in {"GIT_CONFIG_COUNT", "GIT_CONFIG_GLOBAL"}
        ):
            stripped.pop(name)
    return stripped


class CacheDisciplineMixin:
    """Both ambient-config readers are lru_cached on the repository path.

    A cached tuple keyed on a path outlives the home directory that produced
    it, so an arm that ran under one home would answer for the next one.
    """

    def setUp(self) -> None:
        super().setUp()
        git._ambient_worktree_config_overrides.cache_clear()
        git._repository_filter_config_overrides.cache_clear()

    def tearDown(self) -> None:
        git._ambient_worktree_config_overrides.cache_clear()
        git._repository_filter_config_overrides.cache_clear()
        super().tearDown()


class HostileHomeReachesGitTests(CacheDisciplineMixin, unittest.TestCase):
    """OBL-GIT-SOURCE-154 premise: the planted global config really is in front of Git."""

    @classmethod
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        base = Path(directory.name)
        cls.hostile = HostileHome(base / "home-hostile")
        cls.clean = clean_home(base / "home-clean")
        cls.repo = base / "repo"
        cls.repo.mkdir()
        init_git_repo(cls.repo, initial_branch="main")
        drop_local_filemode(cls.repo)
        (cls.repo / "a.txt").write_bytes(b"hello\n")
        (cls.repo / ".gitattributes").write_bytes(b"*.txt filter=evil\n")
        for arguments in (("add", "--all"), ("commit", "-m", "premise")):
            result = plain_git(cls.repo, *arguments)
            assert result.returncode == 0, result.stderr

    def test_every_planted_setting_resolves_at_global_scope(self):
        with home_pointed_at(self.hostile.root):
            for key, origin in PLANTED.items():
                with self.subTest(setting=key, declared_in=origin):
                    result = resolved_setting(self.repo, key)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    scope, source, value = result.stdout.rstrip("\n").split("\t")
                    expected_file, expected_value = self.hostile.declared(key)
                    self.assertEqual(scope, "global")
                    self.assertTrue(source.startswith("file:"), source)
                    self.assertEqual(
                        Path(source[len("file:"):]).resolve(),
                        expected_file.resolve(),
                    )
                    self.assertEqual(value, expected_value)

    def test_no_planted_setting_resolves_under_a_clean_home(self):
        """The control: without it, the settings above could be the real home's."""
        with home_pointed_at(self.clean):
            for key in PLANTED:
                with self.subTest(setting=key):
                    result = resolved_setting(self.repo, key)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr, "")

    def test_plain_git_writes_every_configured_trace_file(self):
        """A trace sink needs no shell, so this premise holds on any host."""
        self.hostile.clear_side_effects()
        with home_pointed_at(self.hostile.root):
            result = plain_git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(result.returncode, 0, result.stderr)
        for key, target in self.hostile.traces.items():
            with self.subTest(sink=key):
                self.assertTrue(target.is_file(), f"{key} wrote nothing")
                self.assertGreater(target.stat().st_size, 0)

    def test_plain_git_writes_no_trace_file_under_a_clean_home(self):
        self.hostile.clear_side_effects()
        with home_pointed_at(self.clean):
            result = plain_git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.hostile.side_effects(), [])

    def test_boundver_own_config_reader_sees_the_hostile_global_file(self):
        """The ambient-worktree allowlist is the one live channel from global scope.

        `core.eol` is used rather than `core.autocrlf` because Git for Windows
        ships a system config that already declares the latter, so a hostile
        value there would be indistinguishable from the baseline.
        """
        resolved = str(self.repo.resolve(strict=False))
        with home_pointed_at(self.hostile.root):
            git._ambient_worktree_config_overrides.cache_clear()
            hostile = git._ambient_worktree_config_overrides(resolved)
        git._ambient_worktree_config_overrides.cache_clear()
        with home_pointed_at(self.clean):
            clean = git._ambient_worktree_config_overrides(resolved)
        self.assertIn(("core.eol", "crlf"), hostile)
        self.assertNotIn(("core.eol", "crlf"), clean)

    def test_a_setting_reachable_only_through_the_include_is_not_promoted(self):
        """Which is what --no-includes on that query buys.

        The reader returns an empty tuple for every failure it swallows: a bad
        `.git` marker, an OSError, a ValueError, a CalledProcessError, any
        stderr at all, an odd field count, an unrecognised key. An absent
        `core.safecrlf` is therefore worth nothing on its own, so the same call
        has to show the query running and promoting first. `core.eol` and
        `core.filemode` are declared in the root file and `core.safecrlf` only
        in the included one. `core.eol` is promoted, `core.filemode` is excluded
        because it can move a digest, and the included key remains invisible.
        """
        resolved = str(self.repo.resolve(strict=False))
        with home_pointed_at(self.hostile.root):
            git._ambient_worktree_config_overrides.cache_clear()
            promoted = dict(git._ambient_worktree_config_overrides(resolved))
        self.assertEqual(promoted.get("core.eol"), "crlf")
        self.assertNotIn("core.filemode", promoted)
        self.assertNotIn("core.safecrlf", promoted)


class ProcessLocalReadBackTests(CacheDisciplineMixin, unittest.TestCase):
    """OBL-GIT-SOURCE-154: what the installed Git resolves under boundver's environment."""

    @classmethod
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        base = Path(directory.name)
        cls.hostile = HostileHome(base / "home-hostile")
        cls.repo = base / "repo"
        cls.repo.mkdir()
        init_git_repo(cls.repo, initial_branch="main")
        (cls.repo / "a.txt").write_bytes(b"hello\n")
        for arguments in (("add", "--all"), ("commit", "-m", "read-back")):
            result = plain_git(cls.repo, *arguments)
            assert result.returncode == 0, result.stderr

    def _read_back(self, executable: str, environment: Dict[str, str], key: str):
        return subprocess.run(
            [executable, "config", "--get", key],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_the_hostile_settings_do_not_survive_into_a_boundver_git_process(self):
        with home_pointed_at(self.hostile.root):
            git._ambient_worktree_config_overrides.cache_clear()
            git._repository_filter_config_overrides.cache_clear()
            environment = git._git_subprocess_env(self.repo)
            executable = git._trusted_git_executable(self.repo)
        for key, expected in SUPPRESSED.items():
            with self.subTest(setting=key):
                result = self._read_back(executable, environment, key)
                if expected is None:
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertEqual(result.stdout, "")
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.rstrip("\n"), expected)

    def test_the_same_query_returns_the_attacker_value_without_the_suppression(self):
        """The control for the read-back above, in the same repository.

        Without it, `filter.evil.clean` returning nothing would be evidence
        that no such driver was ever declared.
        """
        with home_pointed_at(self.hostile.root):
            git._ambient_worktree_config_overrides.cache_clear()
            git._repository_filter_config_overrides.cache_clear()
            environment = strip_process_local(git._git_subprocess_env(self.repo))
            executable = git._trusted_git_executable(self.repo)
        for key in SUPPRESSED:
            with self.subTest(setting=key):
                result = self._read_back(executable, environment, key)
                _, expected = self.hostile.declared(key)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.rstrip("\n"), expected)

    def test_the_environment_pins_every_channel_the_suppression_relies_on(self):
        with home_pointed_at(self.hostile.root):
            environment = git._git_subprocess_env(self.repo)
        for name, value in PINNED_ENVIRONMENT.items():
            with self.subTest(variable=name):
                self.assertEqual(environment.get(name), value)
        count = int(environment["GIT_CONFIG_COUNT"])
        injected = {
            environment[f"GIT_CONFIG_KEY_{index}"]: environment[
                f"GIT_CONFIG_VALUE_{index}"
            ]
            for index in range(count)
        }
        self.assertEqual(injected["safe.directory"], str(self.repo.resolve()))
        # The promotion channel, in the mapping Git actually receives.
        # `core.eol` is declared only in the hostile global file and arrives as
        # a process-local override, which is what makes the digest comparison
        # in PromotedWorktreeSettingTests a statement about a live path rather
        # than about a dormant one. `core.filemode` never arrives from ambient
        # scope because it can change a digest; repository-local declarations
        # remain visible to the Git process itself.
        self.assertEqual(injected["core.eol"], "crlf")
        self.assertNotIn("core.filemode", injected)

    def test_no_ambient_git_variable_reaches_the_subprocess(self):
        """The stripping loop, shown running rather than assumed."""
        with home_pointed_at(self.hostile.root):
            with mock.patch.dict(os.environ, AMBIENT_POISON, clear=False):
                for name, planted in AMBIENT_POISON.items():
                    self.assertEqual(os.environ[name], planted)
                environment = git._git_subprocess_env(self.repo)
        self.assertNotIn("GIT_DIR", environment)
        self.assertNotIn("GIT_EXTERNAL_DIFF", environment)
        self.assertNotIn("SSH_ASKPASS", environment)
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(environment["GIT_TRACE2_EVENT"], "0")


@unittest.skipUnless(
    ALTERNATE_GIT,
    "the obligation asks for every Git in the supported range and this host "
    "installs one; set BOUNDVER_ALTERNATE_GIT to a second Git binary to run it",
)
class AlternateGitReadBackTests(CacheDisciplineMixin, unittest.TestCase):
    """OBL-GIT-SOURCE-154: the same suppression, on a second Git binary."""

    def test_a_second_git_resolves_the_suppressed_values_too(self):
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            hostile = HostileHome(base / "home-hostile")
            repo = base / "repo"
            repo.mkdir()
            init_git_repo(repo, initial_branch="main")
            installed = subprocess.run(
                ["git", "--version"], capture_output=True, text=True
            ).stdout.strip()
            alternate = subprocess.run(
                [ALTERNATE_GIT, "--version"], capture_output=True, text=True
            ).stdout.strip()
            self.assertNotEqual(
                installed, alternate, "BOUNDVER_ALTERNATE_GIT names the same Git"
            )
            with home_pointed_at(hostile.root):
                git._ambient_worktree_config_overrides.cache_clear()
                git._repository_filter_config_overrides.cache_clear()
                environment = git._git_subprocess_env(repo)
            for key, expected in SUPPRESSED.items():
                with self.subTest(setting=key, git=alternate):
                    result = subprocess.run(
                        [ALTERNATE_GIT, "config", "--get", key],
                        cwd=repo,
                        capture_output=True,
                        text=True,
                        env=environment,
                    )
                    if expected is None:
                        self.assertEqual(result.returncode, 1, result.stdout)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.rstrip("\n"), expected)


def build_measurement_repository(scene: Scenario) -> None:
    """A repository with two committed lockfiles, so every command has input.

    `verify`, `status` and `review` at head source read the lockfile out of a
    commit, so a repository carrying only a working-tree lockfile makes them
    all exit 2 with the same message in both arms - equal, and worthless.
    """
    scene.component(
        "svc",
        path="services/svc",
        provider="path-hash",
        boundary=["api/*.yaml"],
        behavior=["api/*.yaml", "src/*.py"],
    )
    scene.file("services/svc/api/v1.yaml", "openapi: 3.1.0\npaths: {}\n")
    scene.file("services/svc/src/app.py", "def run():\n    return 1\n")
    # Repository attributes that route every digested file through the global
    # filter driver, which is how a global config reaches file content.
    scene.file(".gitattributes", "*.yaml filter=evil\n*.py filter=evil\n")
    scene.commit("fixture")
    for message, edit in (("lock", None), ("change", "info: {}\n")):
        if edit is not None:
            scene.append_line("services/svc/api/v1.yaml", edit.rstrip("\n"))
        result = run_cli(scene.root, "generate", "--source", "working-tree")
        assert result.returncode == 0, result.stderr
        scene.git("add", "--all")
        scene.git("commit", "-m", message)


class GlobalConfigSuppressionTests(CacheDisciplineMixin, unittest.TestCase):
    """OBL-GIT-SOURCE-154: a hostile global config changes no output byte and no digest.

    Read the side-effect assertions here for what they are. No single fault in
    the suppression makes `test_the_hostile_arm_left_no_marker_or_trace_file`
    fail: a trace file needs the global config readable *and* the trace2 pins
    gone, and `test_two_faults_are_needed_before_a_trace_file_appears` runs all
    four environments to show it rather than leaving it as a claim. The marker
    half is blunter again, because no allowlisted subcommand asks Git to filter
    a file, which is why the class's own positive control expects three trace
    files and no marker at all. The single-fault detectors for this obligation
    are `ProcessLocalReadBackTests.test_the_hostile_settings_do_not_survive_
    into_a_boundver_git_process` and
    `test_the_environment_pins_every_channel_the_suppression_relies_on`; those
    two are load-bearing, and the assertions below are the outcome check that
    catches a compound regression they would each miss.

    The comparison here also runs on a repository whose local config declares
    `core.filemode`, because `git init` writes it. `AmbientWorktreeSettingTests`
    separately removes that declaration and proves a global replacement still
    cannot move a digest.
    """

    @classmethod
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        base = Path(directory.name)
        cls.hostile = HostileHome(base / "home-hostile")
        cls.clean = clean_home(base / "home-clean")
        cls.scene = Scenario("suppression")
        cls.addClassCleanup(cls.scene.close)
        build_measurement_repository(cls.scene)
        # One repository for every arm: `generate` prints an absolute path, so
        # two scenarios would differ for a reason that is not the home.
        cls.first_clean, _ = cls._arm(cls.clean)
        cls.hostile_arm, cls.hostile_effects = cls._arm(cls.hostile.root)
        cls.second_clean, _ = cls._arm(cls.clean)
        # The positive control for the side-effect assertion, taken against the
        # measured repository itself so it cannot be blamed on a different one.
        cls.hostile.clear_side_effects()
        with home_pointed_at(cls.hostile.root):
            plain_git(cls.scene.root, "rev-parse", "HEAD")
        cls.leak_control = cls.hostile.side_effects()
        cls.hostile.clear_side_effects()

    @classmethod
    def _arm(cls, home: Path) -> Tuple[Dict[str, Tuple], List[str]]:
        cls.hostile.clear_side_effects()
        observed = {}
        with home_pointed_at(home):
            for label, arguments in COMMANDS:
                result = run_cli(cls.scene.root, *arguments)
                lockfile = cls.scene.root / "boundary.lock.json"
                observed[label] = (
                    result.returncode,
                    result.stdout,
                    result.stderr,
                    lockfile.read_bytes() if lockfile.exists() else b"",
                )
        return observed, cls.hostile.side_effects()

    def test_the_clean_arm_actually_did_the_work_it_is_compared_against(self):
        """Two runs that failed the same way would compare equal and prove nothing."""
        for label, (code, stdout, stderr, lockfile) in self.first_clean.items():
            with self.subTest(command=label):
                self.assertEqual(code, 0, f"{label}: {stderr}")
                self.assertNotEqual(lockfile, b"")
                document = json.loads(lockfile.decode("utf-8"))
                exact = document["components"]["svc"]["fingerprints"]["exact"]
                self.assertIsInstance(exact, str)
                self.assertNotEqual(exact, "")
        self.assertIn("Lockfile is up to date.", self.first_clean["verify-head"][1])

    def test_every_command_prints_the_same_bytes_under_a_hostile_global_config(self):
        for label, _ in COMMANDS:
            with self.subTest(command=label):
                clean_code, clean_out, clean_err, _ = self.first_clean[label]
                code, stdout, stderr, _ = self.hostile_arm[label]
                self.assertEqual(code, clean_code)
                self.assertEqual(stdout, clean_out)
                self.assertEqual(stderr, clean_err)

    def test_every_command_writes_the_same_lockfile_under_a_hostile_global_config(self):
        for label, _ in COMMANDS:
            with self.subTest(command=label):
                self.assertEqual(
                    self.hostile_arm[label][3], self.first_clean[label][3]
                )

    def test_a_repeated_clean_arm_reproduces_the_first_one(self):
        """The ordering control: run order must not be readable as a home effect."""
        self.assertEqual(self.second_clean, self.first_clean)

    def test_the_hostile_arm_left_no_marker_or_trace_file(self):
        self.assertEqual(self.hostile_effects, [])

    def test_two_faults_are_needed_before_a_trace_file_appears(self):
        """How much the assertion above is worth, measured instead of assumed.

        Each row takes the environment `_git_subprocess_env` really builds,
        deletes one family of names from the copy, and runs plain Git in the
        measured repository under the measured home. Deleting the config
        redirect leaves the trace2 pins winning; deleting the pins leaves the
        file that names the sinks unreadable; only the fourth row, with both
        gone, writes anything. A reader who takes an empty marker directory as
        proof that one pin is holding is reading more than is there.

        The last step shows the shape cannot be designed away. Git reads
        `trace2.*` from system and global scope only, so a sink declared in the
        repository's own config writes nothing even with every pin deleted, and
        there is no repository-controlled route that would make this assertion
        sensitive to a single fault.
        """
        with home_pointed_at(self.hostile.root):
            git._ambient_worktree_config_overrides.cache_clear()
            git._repository_filter_config_overrides.cache_clear()
            environment = git._git_subprocess_env(self.scene.root)
            executable = git._trusted_git_executable(self.scene.root)
        self.addCleanup(self.hostile.clear_side_effects)
        for label, dropped, expected in TRACE_WEAKENINGS:
            with self.subTest(weakened=label):
                weakened = {
                    name: value
                    for name, value in environment.items()
                    if name not in dropped
                }
                self.hostile.clear_side_effects()
                result = subprocess.run(
                    [executable, "rev-parse", "HEAD"],
                    cwd=self.scene.root,
                    capture_output=True,
                    text=True,
                    env=weakened,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.hostile.side_effects(), expected)

        local_sink = self.hostile.root / "trace2-local.log"
        self.scene.git("config", "trace2.normalTarget", local_sink.as_posix())
        self.addCleanup(self.scene.git, "config", "--unset", "trace2.normalTarget")
        unpinned = {
            name: value
            for name, value in environment.items()
            if name not in {"GIT_TRACE2", "GIT_TRACE2_EVENT", "GIT_TRACE2_PERF"}
        }
        self.hostile.clear_side_effects()
        local = subprocess.run(
            [executable, "rev-parse", "HEAD"],
            cwd=self.scene.root,
            capture_output=True,
            text=True,
            env=unpinned,
        )
        self.assertEqual(local.returncode, 0, local.stderr)
        self.assertFalse(local_sink.exists())

    def test_the_side_effect_check_would_have_seen_a_leak(self):
        """Plain Git in the measured repository fills the same directory.

        Without this, an empty markers directory is free: a suppression that
        had quietly stopped working and one that never had anything to
        suppress look identical.
        """
        self.assertEqual(self.leak_control, TRACE_FILES)

    def test_the_hostile_arm_left_the_index_and_working_tree_untouched(self):
        """A leaked filter renormalizes content, which shows up as a dirty index.

        That premise is not free and is not asserted here, because nothing this
        class runs could dirty the index either way. It is asserted in
        `PlantedExecutableTests`, which shows the same porcelain status empty
        after boundver's whole surface and non-empty after plain Git
        renormalizes the same working tree under the same home. Dirtying it
        takes the planted clean filter, which is a /bin/sh script, so on a host
        with no sh the control skips with the rest of that class and this
        assertion is left uncontrolled.
        """
        self.assertEqual(self.scene.git("status", "--porcelain"), "")


@requires_posix_shell
class PlantedExecutableTests(CacheDisciplineMixin, unittest.TestCase):
    """OBL-GIT-SOURCE-154: the allowlist and the suppression together launch nothing.

    The name of the test below says "together" because that is what the
    measurement supports. Two mechanisms hold the marker files absent, and this
    class measures their joint outcome rather than either one. Suppressing the
    global config stops Git resolving the driver at all; independently, the
    subcommand allowlist means boundver never issues a command that would ask
    Git to filter a file, update a ref or refresh the index, and every offline
    argv additionally carries `-c core.fsmonitor=false`, which outranks
    configuration from any scope. Removing the suppression alone therefore
    still leaves every marker absent, and
    `test_no_allowlisted_subcommand_leaks_what_git_status_leaks` measures that
    directly instead of asserting it: under an environment with the global
    redirect deleted, none of the allowlisted subcommands writes a marker while
    `git status`, which boundver cannot issue, writes one immediately.
    """

    def test_the_allowlist_and_the_suppression_together_run_none_of_the_planted_commands(
        self,
    ):
        """One repository, one home, boundver first and plain Git second.

        Running boundver first means the marker directory it is measured
        against is the same one plain Git then fills, so the negative result
        and the positive control cannot differ in the fixture. The porcelain
        status is taken on both sides of the same seam, which is the control
        `GlobalConfigSuppressionTests` needs for its own empty-status
        assertion: after boundver's whole surface the working tree is clean,
        and after plain Git renormalizes it under the same hostile home the
        leaked filter has rewritten content and the status is not.
        """
        with tempfile.TemporaryDirectory() as name:
            hostile = HostileHome(Path(name) / "home-hostile")
            with Scenario("executables") as scene:
                build_measurement_repository(scene)
                with home_pointed_at(hostile.root):
                    for _, arguments in COMMANDS:
                        result = run_cli(scene.root, *arguments)
                        self.assertEqual(result.returncode, 0, result.stderr)
                    quiet = hostile.side_effects()
                # Both status readings are taken with the hostile home out of
                # the way. Under it, plain Git runs the planted clean filter on
                # every tracked file before comparing, so it reports the whole
                # tree modified whatever boundver did, and the reading would
                # measure the fixture instead of the run.
                quiet_status = scene.git("status", "--porcelain")
                with home_pointed_at(hostile.root):
                    plain = plain_git(scene.root, "status", "--porcelain")
                    self.assertEqual(plain.returncode, 0, plain.stderr)
                    renormalized = plain_git(scene.root, "add", "--renormalize", ".")
                    self.assertEqual(renormalized.returncode, 0, renormalized.stderr)
                    noisy = hostile.side_effects()
                noisy_status = scene.git("status", "--porcelain")
                self.assertEqual(quiet, [])
                self.assertEqual(quiet_status, "")
                self.assertIn("markers/filter-clean", noisy)
                self.assertIn("markers/hooks", noisy)
                self.assertIn("trace2-event.json", noisy)
                self.assertNotEqual(noisy_status, "")

    def test_no_allowlisted_subcommand_leaks_what_git_status_leaks(self):
        """Which mechanism is carrying the absent markers, measured on one home.

        Every command here runs under the same environment with the single
        most load-bearing pin, `GIT_CONFIG_GLOBAL`, deleted, so the hostile
        `~/.gitconfig` is live and its filter driver resolvable. The
        subcommands boundver is allowed to issue still leave the marker
        directory empty; `git status`, which is not in the allowlist, fires the
        planted clean filter. So the absence of a marker in the arm above is
        evidence about boundver's command set, and it is the trace files -
        which `rev-parse` alone can write - that carry evidence about the
        config suppression.

        Both arms run against a tree carrying one modified filtered file. That
        is what makes the contrast fair rather than a race: every allowlisted
        probe reads the index or an object and never working-tree content, so
        dirtying a file cannot make one of them fire, while status now has to
        read that content to answer and therefore has to run the filter.
        """
        with tempfile.TemporaryDirectory() as name:
            hostile = HostileHome(Path(name) / "home-hostile")
            with Scenario("reachability") as scene:
                build_measurement_repository(scene)
                # The positive control below needs `git status` to run the clean
                # filter, and status runs it only when it has to read a file's
                # content to answer. Two stat shortcuts let it answer without
                # reading, and the fixture has to close both.
                #
                # If the size differs from the index entry, status reports the
                # file modified on that alone and never filters it. So the
                # replacement is the SAME LENGTH as the original. If the size
                # matches and the mtime also matches, status calls it clean and
                # again never filters. So the mtime is moved by an hour, which a
                # test can do exactly rather than by racing the clock.
                #
                # Both were found the hard way: a same-length edit raced on
                # mtime, and the size-changing edit written to fix it made the
                # failure more frequent, because it handed status the first
                # shortcut instead of the second.
                dirty = scene.root / "services" / "svc" / "src" / "app.py"
                original = dirty.read_bytes()
                replacement = original.replace(b"return 1", b"return 2")
                self.assertEqual(
                    len(replacement),
                    len(original),
                    "the replacement must be the same length as the original",
                )
                self.assertNotEqual(replacement, original)
                dirty.write_bytes(replacement)
                moved = dirty.stat().st_mtime - 3600
                os.utime(dirty, (moved, moved))
                self.assertIn(
                    "services/svc/src/app.py",
                    scene.git("status", "--porcelain"),
                    "the fixture edit did not leave the tree dirty",
                )
                with home_pointed_at(hostile.root):
                    git._ambient_worktree_config_overrides.cache_clear()
                    git._repository_filter_config_overrides.cache_clear()
                    environment = git._git_subprocess_env(scene.root)
                    executable = git._trusted_git_executable(scene.root)
                    weakened = {
                        name: value
                        for name, value in environment.items()
                        if name != "GIT_CONFIG_GLOBAL"
                    }
                    for arguments in ALLOWLISTED_PROBES:
                        with self.subTest(command=" ".join(arguments)):
                            hostile.clear_side_effects()
                            result = subprocess.run(
                                [executable, *arguments],
                                cwd=scene.root,
                                capture_output=True,
                                text=True,
                                env=weakened,
                            )
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertEqual(hostile.side_effects(), [])
                    hostile.clear_side_effects()
                    outside = subprocess.run(
                        [executable, "status", "--porcelain"],
                        cwd=scene.root,
                        capture_output=True,
                        text=True,
                        env=weakened,
                    )
                    self.assertEqual(outside.returncode, 0, outside.stderr)
                    resolved = subprocess.run(
                        [executable, "config", "--show-origin", "--get",
                         "filter.evil.clean"],
                        cwd=scene.root,
                        capture_output=True,
                        text=True,
                        env=weakened,
                    )
                    self.assertEqual(
                        resolved.returncode,
                        0,
                        "the hostile global config is not reaching Git at all, so "
                        "the filter could not have fired whatever status did. "
                        f"HOME={weakened.get('HOME')!r} "
                        f"USERPROFILE={weakened.get('USERPROFILE')!r} "
                        f"GIT_CONFIG_GLOBAL={weakened.get('GIT_CONFIG_GLOBAL')!r} "
                        f"status={outside.stdout!r} stderr={resolved.stderr!r}",
                    )
                    self.assertIn(
                        "markers/filter-clean",
                        hostile.side_effects(),
                        "the driver resolved but status did not run it: "
                        f"origin={resolved.stdout.strip()!r} "
                        f"status={outside.stdout!r}",
                    )


class AmbientWorktreeSettingTests(CacheDisciplineMixin, unittest.TestCase):
    """OBL-GIT-SOURCE-154: ambient worktree settings cannot move a digest.

    Everything else in this file is about a channel boundver closes. This class
    is about the narrow channel it deliberately leaves open. Six inert
    worktree keys are read from system and global scope and injected into
    process-local config. `core.filemode` is excluded because it can reach a
    fingerprint. Four homes are measured on four freshly built copies of the
    same repository - clean, `core.eol` alone, `core.filemode` alone, and the
    full hostile fixture. `git init` writes `core.filemode` locally on every
    platform, so `_unpinned` removes it to prove the global value still has no
    effect when no repository declaration masks it.
    """

    #: The behaviour-only file marked executable in the index. It is outside the
    #: boundary selector, which is what makes `boundary` the untouched control
    #: beside `exact` and `behavior` when the mode reading changes.
    EXECUTABLE = "services/svc/src/app.py"

    @staticmethod
    def _unpinned(scene: Scenario) -> None:
        """Undo the local pins that mask an ambient value.

        `Scenario` pins core.autocrlf and core.eol locally and writes a
        repository `* -text`; `git init` writes core.filemode. A local value
        masks a global one, so with any of them in place the promotion this
        class is about never fires.
        """
        scene.git("config", "--unset", "core.autocrlf")
        scene.git("config", "--unset", "core.eol")
        (scene.root / ".git" / "info" / "attributes").unlink()
        drop_local_filemode(scene.root)

    @classmethod
    def _measure(cls, home: Path) -> Dict[str, Any]:
        """Build the repository, unpin it, and generate under *home*."""
        with Scenario("promotion") as scene:
            build_measurement_repository(scene)
            cls._unpinned(scene)
            # update-index records the mode without touching the file, so this
            # is the same on Windows and on a POSIX runner.
            scene.git("update-index", "--chmod=+x", cls.EXECUTABLE)
            scene.git("commit", "-m", "executable")
            modes = {}
            for line in scene.git("ls-tree", "-r", "HEAD").splitlines():
                metadata, _, path = line.partition("\t")
                modes[path] = metadata.split(" ")[0]
            on_disk = (scene.root / cls.EXECUTABLE).stat().st_mode
            resolved = str(scene.root.resolve(strict=False))
            with home_pointed_at(home):
                git._ambient_worktree_config_overrides.cache_clear()
                promoted = dict(git._ambient_worktree_config_overrides(resolved))
                git._ambient_worktree_config_overrides.cache_clear()
                digests = {}
                lockfiles = {}
                for label in ("head", "working-tree"):
                    result = run_cli(scene.root, "generate", "--source", label)
                    assert result.returncode == 0, result.stderr
                    raw = (scene.root / "boundary.lock.json").read_bytes()
                    lockfiles[label] = raw
                    document = json.loads(raw.decode("utf-8"))
                    digests[label] = document["components"]["svc"]["fingerprints"]
            return {
                "promoted": promoted,
                "digests": digests,
                "lockfiles": lockfiles,
                "modes": modes,
                "on_disk_mode": on_disk,
            }

    @classmethod
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        base = Path(directory.name)
        cls.clean = cls._measure(clean_home(base / "home-clean"))
        cls.eol_only = cls._measure(
            narrow_home(base / "home-eol", "\teol = crlf\n")
        )
        cls.filemode_only = cls._measure(
            narrow_home(base / "home-filemode", "\tfilemode = false\n")
        )
        cls.hostile = cls._measure(HostileHome(base / "home-hostile").root)

    def test_the_four_homes_differ_only_in_the_keys_they_plant(self):
        """The premise: each arm promoted what it was supposed to and nothing else.

        The system config Git for Windows ships already declares core.autocrlf,
        so the promoted set is never empty and an arm cannot be identified by
        emptiness. Comparing each arm against the clean one key by key says
        which reading is the variable rather than asserting a host-specific
        tuple.
        """
        baseline = self.clean["promoted"]
        self.assertNotIn("core.eol", baseline)
        self.assertNotIn("core.filemode", baseline)
        self.assertEqual(
            self.eol_only["promoted"], {**baseline, "core.eol": "crlf"}
        )
        self.assertEqual(
            self.filemode_only["promoted"], baseline
        )
        self.assertEqual(
            self.hostile["promoted"],
            {**baseline, "core.eol": "crlf"},
        )

    def test_the_fixture_records_a_mode_the_two_readings_disagree_about(self):
        """The second premise: without it, core.filemode has nothing to change.

        `core.filemode=false` means "trust the recorded mode"; unset means
        "stat the file". Those two answers differ only where the recorded mode
        and the file's own permissions disagree, which is what the index chmod
        arranges: HEAD says 100755 while nothing on disk carries an execute
        bit, on Windows because the filesystem has none and on POSIX because
        update-index never touched the file.
        """
        for arm in (self.clean, self.eol_only, self.filemode_only, self.hostile):
            self.assertEqual(arm["modes"][self.EXECUTABLE], "100755")
            self.assertEqual(arm["modes"]["services/svc/api/v1.yaml"], "100644")
            self.assertFalse(
                arm["on_disk_mode"] & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            )

    def test_a_promoted_line_ending_setting_moves_no_digest(self):
        """Scope pin: core.eol is promoted and is inert, at both sources.

        boundver replaces CRLF with LF before hashing and reads head blobs raw,
        so nothing Git's end-of-line conversion could do reaches a fingerprint.
        This is the case the class used to measure on its own, kept as the
        contrast that shows the divergence below is about one key and not about
        promotion as such.
        """
        self.assertEqual(self.eol_only["digests"], self.clean["digests"])
        self.assertEqual(self.eol_only["lockfiles"], self.clean["lockfiles"])

    def test_an_ambient_core_filemode_moves_no_digest(self):
        """A home-directory mode preference is not repository input."""
        self.assertEqual(self.filemode_only["digests"], self.clean["digests"])
        self.assertEqual(self.filemode_only["lockfiles"], self.clean["lockfiles"])
        self.assertEqual(
            self.hostile["digests"], self.eol_only["digests"]
        )
        self.assertEqual(self.hostile["lockfiles"], self.eol_only["lockfiles"])

    def test_a_hostile_global_config_changes_no_lockfile_digest(self):
        """OBL-GIT-SOURCE-154: identical lockfile digests, whatever HOME holds.

        The hostile file combines executable Git settings, an included file,
        and both an inert and a digest-sensitive worktree setting. Suppression
        and the narrowed promotion allowlist keep every lock byte invariant.
        """
        self.assertEqual(self.hostile["digests"], self.clean["digests"])
        self.assertEqual(self.hostile["lockfiles"], self.clean["lockfiles"])


if __name__ == "__main__":  # pragma: no cover
    sys.exit(unittest.main())
