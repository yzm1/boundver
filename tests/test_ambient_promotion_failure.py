"""A refused ambient Git configuration query fails closed.

boundver suppresses system and global Git configuration so that no ambient file
can name an executable, a credential helper or an include path. That leaves a
hole, because a few of those settings are how a checkout compares with its
index, and losing them would change what a working tree looks like for reasons
that have nothing to do with the repository. `_ambient_worktree_config_overrides`
fills the hole by asking Git which of a narrow allowlist have an effective
system or global value, and copying just those back as process-local config.
Asking is the weak point. A refusal must raise rather than return the empty tuple,
which is reserved for the honest answer that no setting needs promotion.

The obligation names an old Git rejecting `--show-scope` as the trigger, and
that trigger cannot be produced here. `--show-scope` has been in Git since
2.26 and `GIT_CONFIG_COUNT` only since 2.31, so a Git that answers exit 129 to
this query would also ignore the channel the promotion writes into, and only
one Git is installed on this host anyway. The refusal is therefore injected at
`_git_run`, and a premise test first makes the installed Git emit a real exit
129 with a real "unknown option" diagnostic for the same argv, so the shape
being injected is measured rather than invented. A second fixture needs no
injection at all: a repository whose own `.git/config` carries enough inert
`core.safecrlf` lines pushes the query's answer past boundver's one-megabyte
output cap and `_git_run` raises a guardrail. That is the version of this
failure a current Git can actually be made to produce, and it is applied
to a repository this file has already read a non-empty promotion out of, so the
padding is demonstrably the only thing that changed between the two readings.

The fixture cannot use `tests._scenarios.Scenario`. Scenario pins core.autocrlf
and core.eol in local config, and a locally declared key is deliberately never
promoted, so under Scenario the healthy answer is empty too and every assertion
here would hold for the wrong reason. It plants a global `.gitconfig` behind
HOME and USERPROFILE instead, creates the repository inside that patched
environment so the ambient config a contributor happens to have cannot decide
what `git init` writes locally, and clears the function's `lru_cache` around
every call, because a second reading is otherwise the first one replayed.

Nothing here may assume the promotion is exactly what the fixture planted.
`_git_config_query_environment` strips every name beginning "GIT_", including
GIT_CONFIG_NOSYSTEM, so the query reads the host's system gitconfig too, and
whatever allowlisted key that file declares is promoted unless `git init`
happens to redeclare it locally. On this host the system file supplies
core.autocrlf=true and core.symlinks=false, and only Git for Windows writing
`symlinks = false` into local config keeps the second one out of the answer; a
POSIX host would promote it. Any test that needs the promotion to be exactly
the two planted keys therefore declares the other five allowlisted keys in
local config first, which masks the host's contribution on every platform, and
what is asserted about the environment is the difference the promotion makes
rather than an absolute count.

Two suppressions make the lost promotion a loss rather than a no-op, and both
are literals in `_git_subprocess_env` that would read back the same with no
repository at all, so this file measures their effect instead of their
presence. The planted global file carries a non-allowlisted `boundver.*` probe
key, and a planted file reachable through GIT_CONFIG_SYSTEM carries another;
each is invisible to real Git under the built environment and visible again
once the one variable that hides it is removed.

Covers OBL-GIT-SOURCE-156.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from typing import Dict, Optional, Tuple
from unittest import mock

from boundver import _git as git
from boundver._git import (
    MAX_GIT_COMMAND_OUTPUT_BYTES,
    MAX_GIT_CONFIG_QUERY_SECONDS,
    _ambient_worktree_config_overrides,
    _git_config_query_environment,
    _git_subprocess_env,
    _repository_filter_config_overrides,
    _SAFE_AMBIENT_WORKTREE_CONFIG_PATTERN,
    _trusted_git_executable,
)
from boundver._utils import GuardrailError

from tests._repo_fixtures import init_git_repo

#: What the fixture plants in the global scope, and therefore what the
#: promotion has to hand back. core.symlinks and core.ignorecase are unusable
#: as the planted key: `git init` writes both into local config on Windows, and
#: a locally declared key is never promoted. core.filemode is intentionally
#: excluded because an ambient value can change a working-tree digest.
AMBIENT = {"core.autocrlf": "true", "core.eol": "crlf"}

#: A key no allowlist pattern matches, planted in the same global file. It can
#: never be promoted, so real Git can only read it back by reading the file,
#: which is how the global suppression is measured rather than asserted.
AMBIENT_PROBE = ("boundver.ambientprobe", "planted")

#: The same trick for system scope, planted in a file the test points
#: GIT_CONFIG_SYSTEM at, because the host's real system gitconfig is not
#: writable and its contents are not this file's to choose.
SYSTEM_PROBE = ("boundver.systemprobe", "planted")

#: An allowlisted key in that same planted system file. It is how this file
#: shows, without depending on what the host's real system gitconfig happens to
#: say, that the promotion query reads system scope and that a system value is
#: promoted unless the repository declares the key itself.
SYSTEM_AMBIENT = ("core.safecrlf", "warn")


def _config_text(settings) -> str:
    """Render a mapping of dotted keys as the Git config file that declares it."""
    sections: Dict[str, list] = {}
    for key, value in settings:
        section, _, name = key.partition(".")
        sections.setdefault(section, []).append(f"\t{name} = {value}\n")
    return "".join(
        f"[{section}]\n" + "".join(lines) for section, lines in sections.items()
    )


GLOBAL_CONFIG = _config_text([*AMBIENT.items(), AMBIENT_PROBE])

SYSTEM_CONFIG = _config_text([SYSTEM_PROBE, SYSTEM_AMBIENT])

#: A valid local value for every allowlisted key. A repository that declares
#: all of them locally genuinely has nothing ambient to promote, which is the
#: honest empty answer this host cannot otherwise produce: its system-scope
#: gitconfig declares core.autocrlf, so an emptied global file is not enough.
LOCAL_DECLARATIONS = {
    "core.autocrlf": "false",
    "core.eol": "lf",
    "core.ignorecase": "true",
    "core.precomposeunicode": "true",
    "core.safecrlf": "false",
    "core.symlinks": "false",
}

#: The same declarations minus the two planted keys. A repository that carries
#: these has exactly AMBIENT left to promote on any host, whatever its own
#: system gitconfig says, because every other allowlisted key is now local.
MASKING_DECLARATIONS = {
    key: value
    for key, value in LOCAL_DECLARATIONS.items()
    if key not in AMBIENT
}

#: One record the promotion query emits for a padded `core.safecrlf` line, NUL
#: framed exactly as Git writes it. The padding is sized from this so the
#: answer clears the output cap without a guessed line count.
PADDING_RECORD = b"local\0core.safecrlf\nfalse\0"

PADDING_LINES = MAX_GIT_COMMAND_OUTPUT_BYTES // len(PADDING_RECORD) + 4096

#: Every way the promotion query can be refused, with the diagnostic each one
#: carries. `_ambient_worktree_config_overrides` must distinguish all of them
#: from the one legitimate empty answer below. The two GuardrailError rows also
#: verify that an already actionable guardrail diagnostic survives unchanged.
REFUSALS = {
    "a Git that has no --show-scope": subprocess.CalledProcessError(
        129,
        ["git", "config"],
        output="",
        stderr="error: unknown option `show-scope'",
    ),
    "a repository-broken .git/config": subprocess.CalledProcessError(
        128,
        ["git", "config"],
        output="",
        stderr="fatal: bad config line 9 in file .git/config",
    ),
    "no git on PATH": FileNotFoundError("git is required"),
    "an argv the offline allowlist refuses": ValueError(
        "Refusing Git invocation outside boundver's offline allowlist"
    ),
    "an answer past the output cap": GuardrailError(
        f"Git command stdout exceeds the {MAX_GIT_COMMAND_OUTPUT_BYTES}-byte limit"
    ),
    "a query past the wall-clock cap": GuardrailError(
        "Git command exceeds the "
        f"{MAX_GIT_CONFIG_QUERY_SECONDS}-second wall-clock limit"
    ),
}

#: The one legitimate empty answer: the installed Git replies exit 1 with both
#: streams empty when the allowlist regex matches nothing.
NO_SETTINGS = subprocess.CalledProcessError(
    1, ["git", "config"], output="", stderr=""
)

def _expected_promotion_argv(resolved_root: str) -> list:
    """The argv the promotion query must be, element for element.

    Every element is derived from the constants the function under test uses,
    so a change to the real query fails the argv premise instead of quietly
    moving what these tests measure. The resolved root is the only part the
    fixture supplies.
    """
    return [
        "-c",
        f"safe.directory={resolved_root}",
        "config",
        "--no-includes",
        "--show-scope",
        "--null",
        "--get-regexp",
        _SAFE_AMBIENT_WORKTREE_CONFIG_PATTERN,
    ]


def _clear_caches() -> None:
    """Both override readers are lru_cached on the resolved root string."""
    _ambient_worktree_config_overrides.cache_clear()
    _repository_filter_config_overrides.cache_clear()


def _refusing_promotion_query(error: BaseException):
    """Refuse the `--show-scope` query only, and run everything else for real."""
    real = git._git_run

    def side_effect(repo_root, args, **kwargs):
        if "--show-scope" in args:
            raise error
        return real(repo_root, args, **kwargs)

    return side_effect


class _Recorder:
    """A stand-in stream, so "nothing was printed" is an assertion not a hope."""

    def __init__(self) -> None:
        self._chunks: list = []

    def write(self, text: str) -> int:
        self._chunks.append(text)
        return len(text)

    def flush(self) -> None:
        return None

    @property
    def text(self) -> str:
        return "".join(self._chunks)


class _LogRecorder(logging.Handler):
    """Every record any logger emits while the capture is open."""

    def __init__(self) -> None:
        super().__init__(level=0)
        self.records: list = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _Channels:
    """Every out-of-band channel a report of the refusal could plausibly use.

    Streams, `warnings` and `logging` together. `_git` binds neither `warnings`
    nor `logging`, and reaches stderr only through a call-time `sys.stderr`
    lookup, so `control()` writes through the module's own `sys` to keep the
    positive control on the path the code under test would actually take.
    """

    def __init__(self) -> None:
        self._patches = []
        self.stdout = _Recorder()
        self.stderr = _Recorder()
        self.logs = _LogRecorder()
        self.warnings: list = []

    def __enter__(self) -> "_Channels":
        self._patches = [
            mock.patch("sys.stdout", self.stdout),
            mock.patch("sys.stderr", self.stderr),
        ]
        for patch in self._patches:
            patch.start()
        self._root = logging.getLogger()
        self._level = self._root.level
        self._root.setLevel(logging.DEBUG)
        self._root.addHandler(self.logs)
        self._warnings = warnings.catch_warnings(record=True)
        self.warnings = self._warnings.__enter__()
        warnings.simplefilter("always")
        return self

    def __exit__(self, *exc: object) -> None:
        self._warnings.__exit__(*exc)
        self._root.removeHandler(self.logs)
        self._root.setLevel(self._level)
        for patch in reversed(self._patches):
            patch.stop()

    def snapshot(self) -> tuple:
        return (
            self.stdout.text,
            self.stderr.text,
            len(self.warnings),
            len(self.logs.records),
        )

    def control(self) -> None:
        """Emit one report on every captured channel, the way `_git` would."""
        git.sys.stdout.write("control on stdout")
        git.sys.stderr.write("control on stderr")
        warnings.warn("control warning", RuntimeWarning, stacklevel=1)
        logging.getLogger("boundver").warning("control record")


SILENT = ("", "", 0, 0)

CONTROLLED = ("control on stdout", "control on stderr", 1, 1)


def _process_local(environment) -> dict:
    """Read GIT_CONFIG_KEY_n/VALUE_n back out as the mapping Git will see."""
    return {
        environment[f"GIT_CONFIG_KEY_{index}"]: environment[
            f"GIT_CONFIG_VALUE_{index}"
        ]
        for index in range(int(environment["GIT_CONFIG_COUNT"]))
    }


class _AmbientRepository:
    """A repository with a genuine system/global setting for Git to report.

    The global file is planted behind HOME and USERPROFILE rather than through
    GIT_CONFIG_GLOBAL, because `_git_config_query_environment` strips every
    name beginning "GIT_" before it runs the query. Everything that runs Git
    happens inside `__enter__`, under that patched environment, so a
    contributor's own `init.templateDir` cannot decide what local config the
    fixture repository ends up with; the local config `git init` writes is
    exactly what masks a system-scope key from the promotion, so that is not a
    detail the measurement can afford to inherit from the host.
    """

    def __init__(
        self,
        *,
        padding_lines: int = 0,
        declare_locally: bool = False,
        mask_other_allowlisted: bool = False,
    ) -> None:
        self._padding_lines = padding_lines
        self._declare_locally = declare_locally
        self._mask_other_allowlisted = mask_other_allowlisted
        self._directory = None
        self._environment = None

    def __enter__(self) -> "_AmbientRepository":
        self._directory = tempfile.TemporaryDirectory()
        try:
            base = Path(self._directory.name)
            self.home = base / "home"
            self.home.mkdir()
            (self.home / ".gitconfig").write_text(GLOBAL_CONFIG, encoding="utf-8")
            self.system_config = base / "systemconfig"
            self.system_config.write_text(SYSTEM_CONFIG, encoding="utf-8")
            self.root = base / "repo"
            self.root.mkdir()
            self._environment = mock.patch.dict(
                os.environ,
                {"HOME": str(self.home), "USERPROFILE": str(self.home)},
                clear=False,
            )
            self._environment.start()
            try:
                os.environ.pop("XDG_CONFIG_HOME", None)
                _clear_caches()
                init_git_repo(self.root)
                self._config = self.root / ".git" / "config"
                self._baseline = self._config.read_text(encoding="utf-8")
                if self._declare_locally:
                    self.declare_allowlist_locally()
                if self._mask_other_allowlisted:
                    self._append(_config_text(MASKING_DECLARATIONS.items()))
                if self._padding_lines:
                    self.pad(self._padding_lines)
            except BaseException:
                self._environment.stop()
                raise
        except BaseException:
            self._directory.cleanup()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        _clear_caches()
        self._environment.stop()
        self._directory.cleanup()

    def _append(self, text: str) -> None:
        self._config.write_text(
            self._config.read_text(encoding="utf-8") + text, encoding="utf-8"
        )
        _clear_caches()

    def reset_config(self) -> None:
        """Put `.git/config` back to what `git init` left, and forget the cache."""
        self._config.write_text(self._baseline, encoding="utf-8")
        _clear_caches()

    def declare_allowlist_locally(self) -> None:
        """Give the repository its own value for every allowlisted key."""
        self.reset_config()
        self._append(_config_text(LOCAL_DECLARATIONS.items()))

    def pad(self, lines: int = PADDING_LINES) -> None:
        """Push the query's answer past the output cap, changing nothing else."""
        self._append("[core]\n" + "\tsafecrlf = false\n" * lines)

    @property
    def resolved(self) -> str:
        return str(self.root.resolve(strict=False))

    @property
    def executable(self) -> str:
        return _trusted_git_executable(self.root)

    def git(self, *arguments: str) -> None:
        subprocess.run(
            [self.executable, *arguments],
            cwd=self.root,
            check=True,
            capture_output=True,
        )

    def aimed_at_the_planted_system_file(self):
        """Give the promotion query a system scope this fixture controls.

        `_git_config_query_environment` strips GIT_CONFIG_SYSTEM along with
        every other name beginning "GIT_", so adding it back to what that
        helper returns is the only way to put a chosen system-scope value in
        front of the function under test. Nothing about the function itself is
        replaced.
        """
        real = git._git_config_query_environment
        target = str(self.system_config)

        def aimed() -> dict:
            environment = real()
            environment["GIT_CONFIG_SYSTEM"] = target
            return environment

        return mock.patch.object(git, "_git_config_query_environment", aimed)

    def captured_query(self) -> dict:
        """The argv and keywords the function under test really hands _git_run.

        Every raw and replayed run below is built from this rather than from a
        hand-copied argument list, so a change to the real query cannot leave
        the premises passing against a command boundver no longer sends.
        """
        recorded: dict = {}

        def record(repo_root, args, **kwargs):
            recorded["target"] = repo_root
            recorded["argv"] = list(args)
            recorded["kwargs"] = dict(kwargs)
            raise FileNotFoundError("git is required")

        _clear_caches()
        with mock.patch.object(git, "_git_run", side_effect=record):
            try:
                _ambient_worktree_config_overrides(self.resolved)
            except GuardrailError:
                pass
        _clear_caches()
        if "argv" not in recorded:
            raise AssertionError("the promotion never reached _git_run")
        return recorded

    def raw_query(
        self, option: str = "--show-scope", pattern: Optional[str] = None
    ) -> subprocess.CompletedProcess:
        """Run the captured promotion argv as raw Git, so bytes can be measured."""
        recorded = self.captured_query()
        argv = recorded["argv"]
        if option != "--show-scope":
            if "--show-scope" not in argv:
                raise AssertionError("the captured argv carries no --show-scope")
            argv = [option if item == "--show-scope" else item for item in argv]
        if pattern is not None:
            if _SAFE_AMBIENT_WORKTREE_CONFIG_PATTERN not in argv:
                raise AssertionError("the captured argv carries no allowlist regex")
            argv = [
                pattern if item == _SAFE_AMBIENT_WORKTREE_CONFIG_PATTERN else item
                for item in argv
            ]
        return subprocess.run(
            [self.executable, *argv],
            cwd=self.root,
            env=recorded["kwargs"]["environment"],
            capture_output=True,
        )

    def promotion_query(self) -> subprocess.CompletedProcess:
        """Replay the captured call through boundver, where guardrails apply."""
        recorded = self.captured_query()
        return git._git_run(
            recorded["target"], recorded["argv"], **recorded["kwargs"]
        )

    def config_get(self, key: str, environment) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.executable, "config", "--get", key],
            cwd=self.root,
            env=environment,
            capture_output=True,
        )

    def overrides(self) -> Tuple[Tuple[str, str], ...]:
        _clear_caches()
        return _ambient_worktree_config_overrides(self.resolved)

    def answer(self, error: BaseException) -> tuple:
        """Everything a caller can observe when the query is refused."""
        _clear_caches()
        with mock.patch.object(
            git, "_git_run", side_effect=_refusing_promotion_query(error)
        ):
            try:
                return ("returned", _ambient_worktree_config_overrides(self.resolved))
            except Exception as raised:
                return ("raised", type(raised).__name__, str(raised))


class AmbientPromotionPremiseTests(unittest.TestCase):
    """OBL-GIT-SOURCE-156: the promotion answers, and the refusals are real."""

    def setUp(self):
        _clear_caches()

    def tearDown(self):
        _clear_caches()

    def test_a_planted_global_setting_is_promoted_into_process_local_config(self):
        """Without this the whole file is compatible with a function returning ()."""
        with _AmbientRepository() as repository:
            overrides = repository.overrides()
            for key, value in AMBIENT.items():
                with self.subTest(setting=key):
                    self.assertIn((key, value), overrides)

    def test_masking_the_rest_of_the_allowlist_leaves_exactly_the_planted_keys(self):
        """The host-independent pin the arithmetic in the caller tests relies on.

        The query reads system scope, so an unmasked promotion is whatever the
        host's system gitconfig contributes plus the plant. With every other
        allowlisted key declared locally, the answer is exactly AMBIENT on any
        host, because a locally declared key is never promoted.
        """
        with _AmbientRepository(mask_other_allowlisted=True) as repository:
            self.assertEqual(
                repository.overrides(), tuple(sorted(AMBIENT.items()))
            )

    def test_the_query_reads_system_scope_and_the_mask_is_what_stops_it(self):
        """Why no assertion here may state the promotion as an absolute count.

        The query runs under an environment with GIT_CONFIG_NOSYSTEM stripped,
        so an allowlisted key in system scope is promoted like any other, and
        which keys those are is a fact about the machine. Pointed at a system
        file this fixture wrote, the plain repository promotes its
        core.safecrlf as well as the two planted global keys, and the masked
        repository does not, because it declares core.safecrlf itself. That is
        the same contribution the host's real system gitconfig makes, made
        visible without depending on what the host's file says.
        """
        key, value = SYSTEM_AMBIENT
        with _AmbientRepository() as repository:
            with repository.aimed_at_the_planted_system_file():
                self.assertIn((key, value), repository.overrides())
        with _AmbientRepository(mask_other_allowlisted=True) as masked:
            with masked.aimed_at_the_planted_system_file():
                self.assertEqual(
                    masked.overrides(), tuple(sorted(AMBIENT.items()))
                )

    def test_the_masking_declarations_cover_the_allowlist_apart_from_the_plant(self):
        """Guard the mask against the allowlist growing underneath it."""
        self.assertEqual(
            set(MASKING_DECLARATIONS) | set(AMBIENT),
            set(git._SAFE_AMBIENT_WORKTREE_CONFIG_VALUES),
        )
        self.assertTrue(set(MASKING_DECLARATIONS).isdisjoint(AMBIENT))

    def test_a_locally_declared_setting_is_not_promoted(self):
        """Which is why this file cannot use Scenario: it pins both keys locally."""
        with _AmbientRepository() as repository:
            repository.git("config", "--local", "core.autocrlf", "false")
            promoted = dict(repository.overrides())
            self.assertNotIn("core.autocrlf", promoted)
            self.assertEqual(promoted.get("core.eol"), AMBIENT["core.eol"])

    def test_a_directory_without_a_git_marker_never_reaches_git(self):
        """The trap a fixture without a repository would fall into.

        The function returns () before running Git at all, so an injected
        refusal would never fire and every assertion below would be about
        nothing. `test_the_refused_call_really_was_the_promotion_query` is the
        other half: on this fixture the injected refusal is reached exactly once.
        """
        with tempfile.TemporaryDirectory() as bare:
            _clear_caches()
            with mock.patch.object(git, "_git_run") as runner:
                answer = _ambient_worktree_config_overrides(
                    str(Path(bare).resolve(strict=False))
                )
            self.assertEqual(answer, ())
            self.assertFalse(runner.called)

    def test_the_fixture_repository_has_the_marker_the_query_needs(self):
        with _AmbientRepository() as repository:
            self.assertTrue((repository.root / ".git").is_dir())

    def test_the_installed_git_answers_the_promotion_query_without_complaint(self):
        with _AmbientRepository() as repository:
            answer = repository.raw_query()
            self.assertEqual(answer.returncode, 0)
            self.assertEqual(answer.stderr, b"")
            for key, value in AMBIENT.items():
                with self.subTest(setting=key):
                    self.assertIn(
                        b"global\0" + f"{key}\n{value}".encode() + b"\0",
                        answer.stdout,
                    )

    def test_the_installed_git_really_rejects_an_unknown_option_with_129(self):
        """The 129 injected below is measured, not invented."""
        with _AmbientRepository() as repository:
            answer = repository.raw_query(option="--show-scope-typo")
            self.assertEqual(answer.returncode, 129)
            self.assertEqual(answer.stdout, b"")
            self.assertEqual(
                answer.stderr.decode().splitlines()[0],
                "error: unknown option `show-scope-typo'",
            )

    def test_a_query_that_matches_nothing_answers_exit_1_in_silence(self):
        """The shape NO_SETTINGS injects: Git's own reply when nothing matches."""
        with _AmbientRepository() as repository:
            answer = repository.raw_query(pattern=r"^nosuchsection\.nosuchkey$")
            self.assertEqual(answer.returncode, 1)
            self.assertEqual(answer.stdout, b"")
            self.assertEqual(answer.stderr, b"")

    def test_a_second_reading_is_the_first_one_replayed_unless_the_cache_clears(self):
        """Why every call here goes through cache_clear first."""
        with _AmbientRepository() as repository:
            first = _ambient_worktree_config_overrides(repository.resolved)
            self.assertIn(("core.autocrlf", "true"), first)
            repository.git("config", "--local", "core.autocrlf", "false")
            self.assertEqual(
                _ambient_worktree_config_overrides(repository.resolved), first
            )
            _clear_caches()
            self.assertNotIn(
                ("core.autocrlf", "true"),
                _ambient_worktree_config_overrides(repository.resolved),
            )


class RejectedQueryTests(unittest.TestCase):
    """OBL-GIT-SOURCE-156: a refused query must not read as an empty answer."""

    def setUp(self):
        _clear_caches()

    def tearDown(self):
        _clear_caches()

    def test_a_rejected_query_is_distinguishable_from_having_nothing_to_promote(self):
        """A refused query fails closed; an honest empty answer remains empty."""
        with _AmbientRepository() as repository:
            self.assertNotEqual(
                repository.answer(REFUSALS["a Git that has no --show-scope"]),
                repository.answer(NO_SETTINGS),
            )

    def test_the_security_floor_refusal_names_installed_and_required_versions(self):
        with mock.patch.object(
            git, "_installed_git_version", return_value=((2, 31, 9), "2.31.9")
        ):
            with self.assertRaises(GuardrailError) as raised:
                git._require_process_local_git_config("repo")
        detail = str(raised.exception)
        self.assertIn("Git 2.31.9", detail)
        self.assertIn("required Git 2.32.0", detail)

    def test_every_refusal_fails_closed(self):
        """Every refusal raises; only Git's silent exit 1 means no settings."""
        with _AmbientRepository() as repository:
            for label, error in REFUSALS.items():
                with self.subTest(refusal=label):
                    observed = repository.answer(error)
                    self.assertEqual(observed[0:2], ("raised", "GuardrailError"))
                    if isinstance(error, GuardrailError):
                        self.assertEqual(observed[2], str(error))
                    else:
                        self.assertEqual(
                            observed[2],
                            "Cannot safely inspect ambient Git worktree configuration",
                        )
            self.assertEqual(repository.answer(NO_SETTINGS), ("returned", ()))

    def test_the_refused_call_really_was_the_promotion_query(self):
        """The premise for every refusal above: Git was asked exactly once.

        The whole argv is pinned, not a sample of it, and every element is
        derived from the constants the function itself uses. The keywords are
        pinned too, because the query runs under the stripped environment that
        carries no process-local block of its own.
        """
        with _AmbientRepository() as repository:
            _clear_caches()
            side_effect = _refusing_promotion_query(
                REFUSALS["a Git that has no --show-scope"]
            )
            with mock.patch.object(
                git, "_git_run", side_effect=side_effect
            ) as runner:
                with self.assertRaises(GuardrailError) as raised:
                    _ambient_worktree_config_overrides(repository.resolved)
            self.assertEqual(
                str(raised.exception),
                "Cannot safely inspect ambient Git worktree configuration",
            )
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(runner.call_args.args[0], Path(repository.resolved))
            self.assertEqual(
                runner.call_args.args[1],
                _expected_promotion_argv(repository.resolved),
            )
            self.assertEqual(
                runner.call_args.kwargs["deadline_seconds"],
                MAX_GIT_CONFIG_QUERY_SECONDS,
            )
            environment = runner.call_args.kwargs["environment"]
            self.assertNotIn("GIT_CONFIG_COUNT", environment)
            self.assertEqual(environment["GIT_NO_LAZY_FETCH"], "1")

    def test_the_captured_argv_is_the_one_the_raw_and_replayed_runs_use(self):
        """The premise for every fixture query: nothing below is hand-copied."""
        with _AmbientRepository() as repository:
            recorded = repository.captured_query()
            self.assertEqual(
                recorded["argv"], _expected_promotion_argv(repository.resolved)
            )
            self.assertEqual(recorded["target"], Path(repository.resolved))
            self.assertEqual(
                sorted(recorded["kwargs"]), ["deadline_seconds", "environment"]
            )
            self.assertEqual(
                recorded["kwargs"]["environment"],
                _git_config_query_environment(),
            )

    def test_no_out_of_band_channel_reports_the_refusal(self):
        """The refusal uses only the exception channel.

        The failure belongs in the exception channel. This test rules out also
        printing it on a stream, warning, or logger. `_git` writes to stderr
        through a call-time `sys.stderr` lookup and binds neither `warnings`
        nor `logging`, so each absence below is followed, inside the same open
        capture, by a control that writes on all four channels and is seen.
        """
        with _AmbientRepository() as repository:
            for label, error in REFUSALS.items():
                with self.subTest(refusal=label):
                    with _Channels() as channels:
                        repository.answer(error)
                        self.assertEqual(channels.snapshot(), SILENT)
                        channels.control()
                        self.assertEqual(channels.snapshot(), CONTROLLED)

    def test_the_module_under_test_resolves_the_streams_the_control_writes_to(self):
        """The premise behind that control: it is not writing to a different sys.

        `_git` reaches stderr as `sys.stderr` at call time (the filesystem
        fallback warning is the only such site), so patching the attribute on
        the `sys` module is enough to see it, and `git.sys` is that module.
        """
        self.assertIs(git.sys, sys)
        with _Channels() as channels:
            self.assertEqual(channels.snapshot(), SILENT)
            channels.control()
            self.assertEqual(channels.snapshot(), CONTROLLED)
        self.assertNotIn("logging", vars(git))
        self.assertNotIn("warnings", vars(git))

    def test_the_sibling_query_uses_the_same_fail_closed_policy(self):
        """Both configuration queries reject an ambiguous answer."""
        with _AmbientRepository() as repository:
            _clear_caches()
            with mock.patch.object(
                git,
                "_git_run",
                side_effect=REFUSALS["a Git that has no --show-scope"],
            ):
                with self.assertRaises(GuardrailError) as raised:
                    _repository_filter_config_overrides(repository.resolved)
            self.assertEqual(
                str(raised.exception),
                "Cannot safely inspect repository Git filter configuration",
            )


class GuardrailRefusalTests(unittest.TestCase):
    """OBL-GIT-SOURCE-156: real Git guardrails also fail closed."""

    def setUp(self):
        _clear_caches()

    def tearDown(self):
        _clear_caches()

    def test_padding_the_same_repository_is_what_refuses_the_promotion(self):
        """One repository, read twice, with the padding as the only edit between.

        The previous shape of this test built a second repository and called it
        a control, which measured nothing about the padding. Here the ambient
        config, the Git binary, the HOME and the repository are all the same
        object across the two readings, and no mock is involved in either.
        """
        with _AmbientRepository() as repository:
            before = repository.overrides()
            for key, value in AMBIENT.items():
                with self.subTest(setting=key):
                    self.assertIn((key, value), before)
            repository.pad()
            with self.assertRaises(GuardrailError) as raised:
                repository.overrides()
            self.assertIn("stdout exceeds", str(raised.exception))

    def test_the_padded_repository_really_overflows_the_output_cap(self):
        """The premise: real Git answers, and boundver refuses to read the answer."""
        with _AmbientRepository(padding_lines=PADDING_LINES) as repository:
            raw = repository.raw_query()
            self.assertEqual(raw.returncode, 0)
            self.assertGreater(len(raw.stdout), MAX_GIT_COMMAND_OUTPUT_BYTES)
            self.assertIn(PADDING_RECORD, raw.stdout)
            with self.assertRaises(GuardrailError) as raised:
                repository.promotion_query()
            self.assertEqual(
                str(raised.exception),
                "Git command stdout exceeds the "
                f"{MAX_GIT_COMMAND_OUTPUT_BYTES}-byte limit",
            )

    def test_the_control_repository_declares_the_whole_allowlist_locally(self):
        """Guard the control against the allowlist growing underneath it."""
        self.assertEqual(
            set(LOCAL_DECLARATIONS), set(git._SAFE_AMBIENT_WORKTREE_CONFIG_VALUES)
        )
        for key, value in LOCAL_DECLARATIONS.items():
            with self.subTest(setting=key):
                self.assertIn(
                    value, git._SAFE_AMBIENT_WORKTREE_CONFIG_VALUES[key]
                )

    def test_the_control_repository_has_nothing_ambient_left_to_promote(self):
        """The premise: () here is an answer Git gave, not one boundver invented."""
        with _AmbientRepository(declare_locally=True) as control:
            raw = control.raw_query()
            self.assertEqual(raw.returncode, 0)
            self.assertEqual(raw.stderr, b"")
            for key, value in LOCAL_DECLARATIONS.items():
                with self.subTest(setting=key):
                    self.assertIn(
                        b"local\0" + f"{key}\n{value}".encode() + b"\0", raw.stdout
                    )
            self.assertEqual(control.overrides(), ())

    def test_an_unreadable_answer_is_distinguishable_from_an_empty_one(self):
        """A current Git can produce both states in one repository.

        This is a refusal a current Git can be made to produce from inside
        the repository, without an old Git and without a patched `_git_run`.
        One repository walks all three states, so the unreadable arm and the
        empty arm differ only in what the repository's own config contains:
        first the promotion is non-empty, then the padding makes the answer
        unreadable, then the same config declares the whole allowlist locally
        and there is honestly nothing to promote.
        """
        with _AmbientRepository() as repository:
            self.assertNotEqual(repository.overrides(), ())
            repository.pad()
            with self.assertRaises(GuardrailError):
                repository.overrides()
            repository.declare_allowlist_locally()
            self.assertEqual(repository.overrides(), ())

    def test_the_overflowing_answer_retains_its_guardrail_diagnostic(self):
        with _AmbientRepository(padding_lines=PADDING_LINES) as repository:
            with self.assertRaises(GuardrailError) as raised:
                repository.overrides()
            self.assertEqual(
                str(raised.exception),
                "Git command stdout exceeds the "
                f"{MAX_GIT_COMMAND_OUTPUT_BYTES}-byte limit",
            )

    def test_an_unreadable_promotion_prevents_building_the_environment(self):
        _clear_caches()
        with _AmbientRepository(padding_lines=PADDING_LINES) as repository:
            _clear_caches()
            with self.assertRaises(GuardrailError) as raised:
                _git_subprocess_env(repository.root)
            self.assertIn("stdout exceeds", str(raised.exception))


class PromotionFailureAtTheCallerTests(unittest.TestCase):
    """OBL-GIT-SOURCE-156: callers cannot use an unverified Git environment."""

    def setUp(self):
        _clear_caches()

    def tearDown(self):
        _clear_caches()

    def _environment_and_refusal(self, repository) -> tuple:
        """Return the promoted environment and the refusal seen by its caller."""
        _clear_caches()
        promoted = _git_subprocess_env(repository.root)
        _clear_caches()
        with mock.patch.object(
            git,
            "_git_run",
            side_effect=_refusing_promotion_query(
                REFUSALS["a Git that has no --show-scope"]
            ),
        ):
            with self.assertRaises(GuardrailError) as raised:
                _git_subprocess_env(repository.root)
        return promoted, str(raised.exception)

    def test_the_promotion_reaches_the_environment_as_process_local_config(self):
        """The premise: the tuple is spliced into GIT_CONFIG_KEY_n/VALUE_n.

        What is asserted is the difference the promotion makes, which is a
        property of the promotion, and then the exact promoted set on a fixture
        that masks the host's own system gitconfig. An absolute count would be
        a property of this machine: the query does not suppress system scope,
        so a host whose system file declares an allowlisted key that `git init`
        does not redeclare locally promotes that key too.
        """
        with _AmbientRepository(mask_other_allowlisted=True) as repository:
            promoted, refusal = self._environment_and_refusal(repository)
            promoted_values = _process_local(promoted)
            self.assertEqual(
                refusal, "Cannot safely inspect ambient Git worktree configuration"
            )
            for key, value in AMBIENT.items():
                with self.subTest(setting=key):
                    self.assertEqual(promoted_values.get(key), value)

    def test_the_installed_git_reads_the_promoted_value_back(self):
        """The premise: the promotion is config Git honours, not a dict entry."""
        with _AmbientRepository() as repository:
            promoted, _refusal = self._environment_and_refusal(repository)
            answer = repository.config_get("core.autocrlf", promoted)
            self.assertEqual(answer.returncode, 0)
            self.assertEqual(answer.stdout.decode().strip(), "true")

    def test_a_refused_promotion_returns_no_partial_environment(self):
        with _AmbientRepository() as repository:
            _promoted, refusal = self._environment_and_refusal(repository)
            self.assertEqual(
                refusal, "Cannot safely inspect ambient Git worktree configuration"
            )

    def test_the_ambient_files_stay_suppressed_after_promotion(self):
        """Promotion copies allowlisted values without exposing ambient files.

        Reading GIT_CONFIG_GLOBAL and GIT_CONFIG_NOSYSTEM back out of the
        environment would prove nothing: both are unconditional literals in
        `_git_subprocess_env`, set before the block that does the promotion, so
        the same two assertions hold for a call with no repository at all. What
        is measured instead is their effect, with a non-allowlisted probe key
        planted in each ambient file so no promotion can supply it. Under both
        environments real Git cannot see either probe; remove the one variable
        that hides each, and the same Git reads it straight out of the file
        that was there the whole time. System scope needs GIT_CONFIG_SYSTEM to
        point at a writable file, because the host's own system gitconfig is
        neither writable nor this file's to choose.
        """
        global_key, global_value = AMBIENT_PROBE
        system_key, system_value = SYSTEM_PROBE
        with _AmbientRepository() as repository:
            environment, _refusal = self._environment_and_refusal(repository)
            self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
            self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")

            aimed = dict(environment)
            aimed["GIT_CONFIG_SYSTEM"] = str(repository.system_config)
            for key in (global_key, system_key):
                with self.subTest(probe=key):
                    hidden = repository.config_get(key, aimed)
                    self.assertEqual(hidden.returncode, 1)
                    self.assertEqual(hidden.stdout, b"")

            unsuppressed = dict(aimed)
            del unsuppressed["GIT_CONFIG_GLOBAL"]
            readable = repository.config_get(global_key, unsuppressed)
            self.assertEqual(readable.returncode, 0)
            self.assertEqual(readable.stdout.decode().strip(), global_value)

            unsuppressed = dict(aimed)
            del unsuppressed["GIT_CONFIG_NOSYSTEM"]
            readable = repository.config_get(system_key, unsuppressed)
            self.assertEqual(readable.returncode, 0)
            self.assertEqual(readable.stdout.decode().strip(), system_value)

    def test_the_probe_keys_are_outside_the_promotion_allowlist(self):
        """The premise for that measurement: no promotion could have supplied them.

        A probe the promotion could copy into process-local config would read
        back under the built environment for the wrong reason, and the
        suppression would look broken rather than measured.
        """
        for key, _value in (AMBIENT_PROBE, SYSTEM_PROBE):
            with self.subTest(probe=key):
                self.assertNotIn(key, git._SAFE_AMBIENT_WORKTREE_CONFIG_VALUES)
        with _AmbientRepository() as repository:
            promoted = dict(repository.overrides())
            self.assertNotIn(AMBIENT_PROBE[0], promoted)
            self.assertNotIn(SYSTEM_PROBE[0], promoted)


if __name__ == "__main__":
    unittest.main()
