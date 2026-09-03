"""Git helper primitives for boundver."""

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import threading
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import (
    BinaryIO,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Tuple,
)

from ._config_contract import git_tag_prefix_error
from ._utils import (
    GuardrailError,
    SOURCE_MODE_SET,
    _bounded_diagnostic_repr,
    _bounded_sorted_paths,
    _is_windows_reparse_point,
    _iter_bounded_filesystem_paths,
    _match_path_glob,
    _match_text_glob,
    _read_bounded_path_bytes,
    _validate_glob_pattern_complexity,
)


MAX_GIT_BLOB_BYTES = 50 * 1024 * 1024
MAX_GIT_BATCH_BYTES = 256 * 1024 * 1024
# Repository-wide change reporting spans multiple independently bounded
# components, so it needs a distinct I/O budget. This cap applies separately
# to the immutable base stream and local worktree reads.
MAX_GIT_REPOSITORY_SCAN_BYTES = 16 * 1024 * 1024 * 1024
MAX_GIT_BATCH_HEADER_BYTES = 64 * 1024
MAX_GIT_TREE_ENTRIES = 50_000
MAX_GIT_STATUS_PATHS = 50_000
MAX_GIT_STATUS_FIELDS = MAX_GIT_STATUS_PATHS * 3
MAX_GIT_PATH_BYTES = 16 * 1024
MAX_GIT_TOTAL_PATH_BYTES = 16 * 1024 * 1024
# Includes ls-tree's mode/type/object header and NUL framing in addition to
# path bytes.  The path-specific limits below are the normative contract; this
# transport limit prevents malformed or unexpectedly verbose Git output from
# consuming memory before a complete record can be validated.
MAX_GIT_LIST_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_GIT_LIST_RECORD_BYTES = MAX_GIT_PATH_BYTES + 512
MAX_GIT_COMMAND_OUTPUT_BYTES = 1024 * 1024
MAX_GIT_DIAGNOSTIC_BYTES = 64 * 1024
MAX_GIT_FAILURE_DETAIL_CHARS = 4096
MAX_GIT_COMMAND_SECONDS = 300
MAX_GIT_CONFIG_QUERY_SECONDS = 10
MAX_GIT_FILTER_DRIVERS = 64
MAX_GIT_FILTER_CONFIG_KEYS = 4 * MAX_GIT_FILTER_DRIVERS
MAX_GIT_FILTER_KEY_BYTES = 512
MAX_GIT_FILTER_OVERRIDE_BYTES = 16 * 1024
_GIT_STREAM_CHUNK_BYTES = 64 * 1024
MAX_FALLBACK_FILES = 50_000
MAX_FALLBACK_TRAVERSAL_ENTRIES = 200_000
MAX_GITIGNORE_BYTES = 1024 * 1024
MAX_GITIGNORE_PATTERN_BYTES = MAX_GIT_PATH_BYTES
MAX_GITIGNORE_RULES = 10_000
MAX_GITIGNORE_MATCH_STEPS = 10_000_000

_GIT_AMBIENT_OVERRIDE_NAMES = frozenset({"SSH_ASKPASS"})
_GIT_AMBIENT_OVERRIDE_PREFIXES = ("GIT_",)

# These settings alter how Git compares a checked-out worktree with its index,
# but cannot name executables, callbacks, include files, or credential/network
# helpers. Repository/worktree values remain visible through local config.
# When the effective value comes only from a system or global scope, copy this
# narrow allowlist into process-local config before suppressing those ambient
# files. This preserves ordinary CRLF/symlink/case semantics without reopening
# the code-execution and redirection surface of arbitrary Git configuration.
_SAFE_AMBIENT_WORKTREE_CONFIG_VALUES = {
    "core.autocrlf": frozenset({"true", "false", "input"}),
    "core.eol": frozenset({"lf", "crlf", "native"}),
    "core.filemode": frozenset({"true", "false"}),
    "core.ignorecase": frozenset({"true", "false"}),
    "core.precomposeunicode": frozenset({"true", "false"}),
    "core.safecrlf": frozenset({"true", "false", "warn"}),
    "core.symlinks": frozenset({"true", "false"}),
}
_SAFE_AMBIENT_WORKTREE_CONFIG_PATTERN = (
    r"^(core\.autocrlf|core\.eol|core\.filemode|core\.ignorecase|"
    r"core\.precomposeunicode|core\.safecrlf|core\.symlinks)$"
)
_FILTER_COMMAND_CONFIG_PATTERN = (
    r"^filter\..*\.(clean|smudge|process|required)$"
)
_FILTER_COMMAND_SUFFIXES = ("clean", "smudge", "process", "required")

# Keep every Git subprocess local.  This allowlist is deliberately enforced
# before process creation so a future caller cannot accidentally turn a local
# repository inspection into fetch, push, ls-remote, or another network-capable
# Git operation.
_OFFLINE_GIT_SUBCOMMANDS = frozenset(
    {
        "cat-file",
        "check-ref-format",
        "config",
        "describe",
        "diff",
        "diff-tree",
        "ls-files",
        "ls-tree",
        "merge-base",
        "rev-list",
        "rev-parse",
        "show-ref",
        "symbolic-ref",
        "write-tree",
    }
)
_OFFLINE_GIT_GLOBAL_OPTIONS = frozenset({"--literal-pathspecs"})


def _offline_git_command(repo_root: Path, args: List[str]) -> List[str]:
    """Build a Git argv only for a statically approved local subcommand."""
    command_index = 0
    while command_index < len(args):
        argument = args[command_index]
        if argument in _OFFLINE_GIT_GLOBAL_OPTIONS:
            command_index += 1
            continue
        if argument == "-c":
            if (
                command_index + 1 >= len(args)
                or not args[command_index + 1].startswith("safe.directory=")
            ):
                raise ValueError(
                    "Refusing unsafe command-scoped Git configuration"
                )
            command_index += 2
            continue
        break
    if (
        command_index >= len(args)
        or args[command_index] not in _OFFLINE_GIT_SUBCOMMANDS
    ):
        raise ValueError(
            "Refusing Git invocation outside boundver's offline allowlist"
        )
    safe_args = list(args)
    subcommand = safe_args[command_index]
    subcommand_arguments = safe_args[command_index + 1 :]
    if subcommand == "cat-file" and any(
        argument in {"--filters", "--textconv"}
        or argument.startswith("--batch-command")
        for argument in subcommand_arguments
    ):
        raise ValueError("Refusing Git cat-file conversion or command mode")
    if subcommand == "ls-files" and any(
        argument == "--recurse-submodules"
        or argument.startswith("--recurse-submodules=")
        for argument in subcommand_arguments
    ):
        raise ValueError("Refusing recursive Git submodule inspection")
    rev_list_arguments = (
        subcommand_arguments[: subcommand_arguments.index("--")]
        if subcommand == "rev-list" and "--" in subcommand_arguments
        else subcommand_arguments
    )
    if subcommand == "rev-list" and any(
        argument == "--show-signature"
        or argument.startswith("--show-signature=")
        or argument == "--pretty"
        or argument.startswith("--pretty=")
        or argument == "--format"
        or argument.startswith("--format=")
        or "%G" in argument
        for argument in rev_list_arguments
    ):
        raise ValueError("Refusing Git signature display or pretty formatting")
    if subcommand in {"diff", "diff-tree"} and any(
        argument in {"--ext-diff", "--textconv", "--no-index"}
        or argument.startswith("--submodule")
        for argument in subcommand_arguments
    ):
        raise ValueError("Refusing Git diff helper or submodule execution mode")
    if subcommand == "diff":
        diff_arguments = subcommand_arguments
        pre_pathspec = (
            diff_arguments[: diff_arguments.index("--")]
            if "--" in diff_arguments
            else diff_arguments
        )
        revisions = [
            argument
            for argument in pre_pathspec
            if not argument.startswith("-")
        ]
        if "--cached" not in pre_pathspec and len(revisions) < 2:
            raise ValueError(
                "Refusing Git worktree diff because repository clean filters "
                "can execute external commands"
            )
    if subcommand in {"diff", "diff-tree"}:
        safe_args[command_index + 1 : command_index + 1] = [
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=all",
        ]
    return _git_command(
        repo_root,
        "--no-pager",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "log.showSignature=false",
        *safe_args,
    )


def _offline_git_environment(
    repo_root: Optional[Path] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Return a Git environment that cannot emit traces or lazy-fetch objects."""
    if environment is None:
        return _git_subprocess_env(repo_root)
    sanitized = dict(environment)
    for name in tuple(sanitized):
        canonical = name.upper()
        if (
            canonical.startswith("GIT_TRACE")
            or canonical.startswith("GIT_EXTERNAL_DIFF")
        ):
            sanitized.pop(name)
    sanitized["GIT_NO_LAZY_FETCH"] = "1"
    sanitized["GIT_TERMINAL_PROMPT"] = "0"
    # Trace2 falls back to trace2.*Target values from system/global config when
    # its environment variables are absent. Explicit false values take
    # precedence and prevent configured file or Unix-socket sinks from
    # receiving repository paths and argv.
    sanitized["GIT_TRACE2"] = "0"
    sanitized["GIT_TRACE2_EVENT"] = "0"
    sanitized["GIT_TRACE2_PERF"] = "0"
    return sanitized


def _git_text_encoding() -> str:
    """Return the encoding Python uses to transport filesystem arguments."""
    return sys.getfilesystemencoding() or "utf-8"


def _decode_git_text(data: bytes, *, successful: bool) -> str:
    """Decode bounded Git text without losing bytes from successful output.

    Git ref names and repository paths use the platform filesystem transport,
    not the user's preferred text locale.  ``surrogateescape`` therefore
    preserves any byte the filesystem codec cannot represent while ordinary
    Unicode output remains readable.  Failed-command diagnostics deliberately
    render undecodable bytes as escapes so they remain terminal-safe.
    """
    errors = "surrogateescape" if successful else "backslashreplace"
    return (
        data.decode(_git_text_encoding(), errors=errors)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )


def _require_git_process_pipes(
    process: subprocess.Popen, *names: str
) -> None:
    """Fail closed if a requested subprocess pipe was not created."""
    missing = [name for name in names if getattr(process, name, None) is None]
    if not missing:
        return
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise RuntimeError(
        "Git subprocess did not expose its requested " + "/".join(missing) + " pipe"
    )


class _BoundedGitDiagnosticDrain:
    """Drain one Git stderr pipe without an unbounded memory or disk spool.

    Streaming stdout paths cannot call ``communicate()`` and historically sent
    stderr to a temporary file to avoid a pipe deadlock. Merely truncating that
    file when reading it back did not bound what a hostile/corrupt repository
    could make Git write first. This concurrent drain retains at most the
    diagnostic ceiling and terminates the child on the one-byte sentinel.
    """

    def __init__(self, process: subprocess.Popen) -> None:
        _require_git_process_pipes(process, "stderr")
        self.process = process
        self.stream: BinaryIO = process.stderr
        self.limit = MAX_GIT_DIAGNOSTIC_BYTES
        self._data = bytearray()
        self._overflow = False
        self._error: Optional[BaseException] = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        try:
            self._thread.start()
        except BaseException:
            try:
                self.stream.close()
            finally:
                try:
                    process.kill()
                except OSError:
                    pass
            raise

    def _drain(self) -> None:
        try:
            while True:
                with self._lock:
                    remaining = self.limit - len(self._data)
                chunk = self.stream.read(
                    min(_GIT_STREAM_CHUNK_BYTES, remaining + 1)
                )
                if not chunk:
                    return
                if not isinstance(chunk, bytes):
                    raise TypeError("Git stderr returned non-byte data")
                if len(chunk) > remaining:
                    with self._lock:
                        self._data.extend(chunk[:remaining])
                        self._overflow = True
                    try:
                        self.process.kill()
                    except OSError:
                        pass
                    return
                with self._lock:
                    self._data.extend(chunk)
        except BaseException as exc:
            with self._lock:
                self._error = exc
            try:
                self.process.kill()
            except OSError:
                pass
        finally:
            self.stream.close()

    def snapshot(self) -> bytes:
        """Return the bounded prefix available without waiting for process exit."""
        with self._lock:
            return bytes(self._data)

    def finish(self) -> bytes:
        """Join after process exit and surface overflow/read failures."""
        self._thread.join()
        with self._lock:
            error = self._error
            overflow = self._overflow
            diagnostic = bytes(self._data)
        if error is not None:
            raise error
        if overflow:
            raise GuardrailError(
                "Git command stderr exceeds the "
                f"{self.limit}-byte limit"
            )
        return diagnostic


class _GitProcessDeadline:
    """Kill one Git process if repository input makes it stop making progress."""

    def __init__(
        self,
        process: subprocess.Popen,
        seconds: Optional[float] = None,
    ) -> None:
        self.process = process
        self.seconds = MAX_GIT_COMMAND_SECONDS if seconds is None else seconds
        self.expired = threading.Event()
        self.timer = threading.Timer(self.seconds, self._expire)
        self.timer.daemon = True

    def _expire(self) -> None:
        try:
            if self.process.poll() is not None:
                return
            self.expired.set()
            self.process.kill()
        except (OSError, ValueError):
            # A concurrent normal reap won the race with the deadline.
            return

    def start(self) -> None:
        try:
            self.timer.start()
        except BaseException:
            try:
                if self.process.poll() is None:
                    self.process.kill()
            except (OSError, ValueError):
                pass
            try:
                self.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired, ValueError):
                pass
            raise

    def cancel(self) -> None:
        self.timer.cancel()

    def raise_if_expired(self) -> None:
        if self.expired.is_set():
            raise GuardrailError(
                "Git command exceeds the "
                f"{self.seconds}-second wall-clock limit"
            )


def _git_failure_detail(exc: BaseException) -> str:
    """Render one terminal-safe, bounded Git or OS failure diagnostic."""
    if isinstance(exc, subprocess.CalledProcessError):
        diagnostic = exc.stderr
        if isinstance(diagnostic, bytes):
            diagnostic = _decode_git_text(
                diagnostic[: MAX_GIT_FAILURE_DETAIL_CHARS + 1],
                successful=False,
            )
        elif isinstance(diagnostic, str):
            diagnostic = diagnostic[: MAX_GIT_FAILURE_DETAIL_CHARS + 1]
        else:
            diagnostic = ""
        diagnostic = diagnostic.strip()
        returncode = _bounded_diagnostic_repr(exc.returncode, max_chars=64)
        if diagnostic:
            rendered = _bounded_diagnostic_repr(
                diagnostic,
                max_chars=MAX_GIT_FAILURE_DETAIL_CHARS,
            )
            return f"return code {returncode}; stderr={rendered}"
        return f"return code {returncode}; no stderr diagnostic"
    if isinstance(exc, OSError):
        detail = str(exc)[: MAX_GIT_FAILURE_DETAIL_CHARS + 1]
        return (
            f"{type(exc).__name__}: "
            f"{_bounded_diagnostic_repr(detail, max_chars=MAX_GIT_FAILURE_DETAIL_CHARS)}"
        )
    return type(exc).__name__


def _git_error_stream_is_empty(value: object) -> bool:
    """Return whether a failed Git command emitted no captured stream data."""
    return value is None or (
        isinstance(value, (bytes, str)) and not value.strip()
    )


def _validated_git_object_id(value: str, operation: str) -> str:
    """Return one bounded Git object ID or fail before it reaches another argv."""
    oid = value.strip()
    if len(oid) not in {40, 64} or any(
        character not in "0123456789abcdefABCDEF" for character in oid
    ):
        raise ValueError(
            f"{operation} returned a malformed object ID: "
            f"{_bounded_diagnostic_repr(oid, max_chars=256)}"
        )
    return oid


def _validated_symbolic_head_ref(value: str) -> str:
    """Return one bounded symbolic HEAD ref before passing it to another argv."""
    ref = value.rstrip("\n")
    if (
        not ref.startswith("refs/heads/")
        or ref == "refs/heads/"
        or len(ref) > MAX_GIT_FAILURE_DETAIL_CHARS
        or ref != ref.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in ref)
    ):
        raise ValueError(
            "git symbolic-ref returned a malformed HEAD ref: "
            f"{_bounded_diagnostic_repr(ref, max_chars=256)}"
        )
    return ref


def _head_is_provably_unborn(repo_root: Path) -> bool:
    """Return true only when symbolic HEAD names a ref that does not exist."""
    try:
        symbolic = _git_run(repo_root, ["symbolic-ref", "--quiet", "HEAD"])
    except subprocess.CalledProcessError as exc:
        if (
            exc.returncode == 1
            and _git_error_stream_is_empty(exc.stdout)
            and _git_error_stream_is_empty(exc.stderr)
        ):
            # Detached HEAD is not unborn; failure to peel it is operational.
            return False
        raise ValueError(
            "git symbolic-ref failed while classifying unresolved HEAD "
            f"({_git_failure_detail(exc)})"
        ) from exc
    except OSError as exc:
        raise ValueError(
            "git symbolic-ref failed while classifying unresolved HEAD "
            f"({_git_failure_detail(exc)})"
        ) from exc

    head_ref = _validated_symbolic_head_ref(symbolic.stdout)
    try:
        _git_run(repo_root, ["check-ref-format", head_ref])
    except subprocess.CalledProcessError as exc:
        raise ValueError(
            "git symbolic-ref returned an invalid HEAD branch ref "
            f"{_bounded_diagnostic_repr(head_ref)} "
            f"({_git_failure_detail(exc)})"
        ) from exc
    except OSError as exc:
        raise ValueError(
            "git check-ref-format failed while classifying symbolic HEAD "
            f"{_bounded_diagnostic_repr(head_ref)} "
            f"({_git_failure_detail(exc)})"
        ) from exc
    try:
        _git_run(
            repo_root,
            ["show-ref", "--verify", "--quiet", "--", head_ref],
        )
    except subprocess.CalledProcessError as exc:
        if (
            exc.returncode == 1
            and _git_error_stream_is_empty(exc.stdout)
            and _git_error_stream_is_empty(exc.stderr)
        ):
            return True
        raise ValueError(
            f"git show-ref failed while checking symbolic HEAD {head_ref!r} "
            f"({_git_failure_detail(exc)})"
        ) from exc
    except OSError as exc:
        raise ValueError(
            f"git show-ref failed while checking symbolic HEAD {head_ref!r} "
            f"({_git_failure_detail(exc)})"
        ) from exc
    return False


@dataclass(frozen=True)
class GitTreeEntry:
    """One immutable path entry in a captured Git tree."""

    path: str
    mode: str
    object_type: str
    oid: str


@dataclass(frozen=True)
class GitSourceSnapshot:
    """Immutable Git-backed source used by one generate/verify operation.

    ``tree_oid`` is either the tree reached from one captured HEAD commit or a
    tree written from the index once. Reading blobs by object ID prevents a
    concurrent ref/index update from producing a hybrid lockfile.

    ``tracked_paths`` additionally freezes index membership for working-tree
    operations. It includes intent-to-add paths, which have no blob in the
    written tree and therefore cannot appear in ``entries``.

    ``skip_worktree_paths`` records materialized index entries that a sparse
    checkout intentionally leaves absent. Working-tree comparisons treat an
    absent path in this set as its captured index identity, matching Git's
    sparse-checkout semantics without consulting repository-defined filters.
    """

    source: str
    tree_oid: str
    entries: Dict[str, GitTreeEntry]
    head_oid: Optional[str] = None
    filemode: bool = True
    tracked_paths: FrozenSet[str] = frozenset()
    skip_worktree_paths: FrozenSet[str] = frozenset()

    def __post_init__(self) -> None:
        # Compatibility snapshots constructed by callers historically only
        # supplied ``entries``. Keep every materialized tree entry tracked
        # while allowing captured index snapshots to add intent-to-add names.
        tracked_paths = frozenset(self.entries) | frozenset(self.tracked_paths)
        skip_worktree_paths = frozenset(self.skip_worktree_paths)
        if not skip_worktree_paths <= frozenset(self.entries):
            raise ValueError(
                "Skip-worktree paths must have materialized captured index entries"
            )
        object.__setattr__(self, "tracked_paths", tracked_paths)
        object.__setattr__(self, "skip_worktree_paths", skip_worktree_paths)


@dataclass(frozen=True)
class _IndexMembership:
    """One bounded capture of tracked and sparse index path state."""

    tracked_paths: FrozenSet[str]
    skip_worktree_paths: FrozenSet[str]


def _filesystem_git_root(start: Path) -> Path:
    """Find the nearest plain ``.git`` marker without consulting Git config."""
    current = start.resolve(strict=True)
    if not current.is_dir():
        current = current.parent
    for candidate in (current, *current.parents):
        marker = candidate / ".git"
        try:
            identity = marker.lstat()
        except FileNotFoundError:
            continue
        if _is_windows_reparse_point(identity):
            raise ValueError(
                "Git marker must not be a symlink, junction, or reparse point: "
                f"{marker}"
            )
        if stat.S_ISDIR(identity.st_mode) or stat.S_ISREG(identity.st_mode):
            return candidate
        raise ValueError(f"Git marker is not a plain file or directory: {marker}")
    raise ValueError(f"No Git worktree contains {start}")


def git_root() -> Path:
    """Find a worktree root without allowing local config to redirect it."""
    expected = _filesystem_git_root(Path.cwd())
    result = _git_run(expected, ["rev-parse", "--show-toplevel"])
    reported = Path(result.stdout.strip()).resolve(strict=True)
    if reported != expected:
        raise ValueError(
            "Git reported a worktree outside the nearest .git marker: "
            f"{reported} != {expected}"
        )
    return expected


def _git_config_query_environment() -> Dict[str, str]:
    """Return an inert environment for reading a narrow Git config allowlist."""
    environment = os.environ.copy()
    for name in tuple(environment):
        canonical = name.upper()
        if canonical in _GIT_AMBIENT_OVERRIDE_NAMES or canonical.startswith(
            _GIT_AMBIENT_OVERRIDE_PREFIXES
        ):
            environment.pop(name, None)
    environment.update(
        {
            "GCM_INTERACTIVE": "never",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_TRACE2": "0",
            "GIT_TRACE2_EVENT": "0",
            "GIT_TRACE2_PERF": "0",
        }
    )
    return environment


@lru_cache(maxsize=128)
def _ambient_worktree_config_overrides(
    resolved_repo_root: str,
) -> Tuple[Tuple[str, str], ...]:
    """Return safe worktree settings lost when ambient config is suppressed.

    ``git config`` only parses configuration; ``--no-includes`` prevents a
    repository-controlled include path from expanding that read. The static
    regex selects settings that affect checkout/index comparison but cannot
    name commands or files. Local/worktree values need no copy because the
    hardened Git invocation still reads repository config; only an effective
    system/global value is promoted into process-local config.
    """
    repo_root = Path(resolved_repo_root)
    try:
        marker = (repo_root / ".git").lstat()
    except OSError:
        return ()
    if (
        _is_windows_reparse_point(marker)
        or not (
            stat.S_ISDIR(marker.st_mode) or stat.S_ISREG(marker.st_mode)
        )
    ):
        return ()
    try:
        result = _git_run(
            repo_root,
            [
                "-c",
                f"safe.directory={resolved_repo_root}",
                "config",
                "--no-includes",
                "--show-scope",
                "--null",
                "--get-regexp",
                _SAFE_AMBIENT_WORKTREE_CONFIG_PATTERN,
            ],
            environment=_git_config_query_environment(),
            deadline_seconds=MAX_GIT_CONFIG_QUERY_SECONDS,
        )
    except (OSError, ValueError, subprocess.CalledProcessError):
        return ()
    if result.stderr.strip():
        return ()
    fields = result.stdout.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 2:
        return ()

    effective: Dict[str, Tuple[str, str]] = {}
    for index in range(0, len(fields), 2):
        scope = fields[index].casefold()
        key, separator, value = fields[index + 1].partition("\n")
        key = key.casefold()
        if not separator or key not in _SAFE_AMBIENT_WORKTREE_CONFIG_VALUES:
            return ()
        # Record even an invalid value so a later local declaration cannot be
        # masked by an earlier valid ambient value.
        effective[key] = (scope, value.casefold())

    overrides = []
    for key, (scope, value) in effective.items():
        if (
            scope in {"system", "global"}
            and value in _SAFE_AMBIENT_WORKTREE_CONFIG_VALUES[key]
        ):
            overrides.append((key, value))
    return tuple(sorted(overrides))


@lru_cache(maxsize=128)
def _repository_filter_config_overrides(
    resolved_repo_root: str,
) -> Tuple[Tuple[str, str], ...]:
    """Return process-local settings that neutralize active Git filters.

    Git invokes ``filter.<driver>.clean`` while commands such as ``status``
    compare a working-tree file with the index. Repository and worktree config
    are therefore executable input unless every active driver is overridden.
    Querying config names is inert; included local config is inspected because
    the subsequent Git command would include it as well. System, global, and
    ambient process config remain suppressed during the query.
    """
    repo_root = Path(resolved_repo_root)
    try:
        marker = (repo_root / ".git").lstat()
    except OSError:
        return ()
    if _is_windows_reparse_point(marker) or not (
        stat.S_ISDIR(marker.st_mode) or stat.S_ISREG(marker.st_mode)
    ):
        return ()

    environment = _git_config_query_environment()
    environment.update(
        {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": os.devnull,
            "GIT_CONFIG_KEY_1": "core.fsmonitor",
            "GIT_CONFIG_VALUE_1": "false",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    try:
        result = _git_run(
            repo_root,
            [
                "config",
                "--includes",
                "--null",
                "--name-only",
                "--get-regexp",
                _FILTER_COMMAND_CONFIG_PATTERN,
            ],
            environment=environment,
            deadline_seconds=MAX_GIT_CONFIG_QUERY_SECONDS,
        )
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1 and not exc.output and not exc.stderr:
            return ()
        raise GuardrailError(
            "Cannot safely inspect repository Git filter configuration"
        ) from exc
    except (OSError, ValueError) as exc:
        raise GuardrailError(
            "Cannot safely inspect repository Git filter configuration"
        ) from exc

    if result.stderr.strip():
        raise GuardrailError(
            "Repository Git filter configuration produced an ambiguous diagnostic"
        )
    raw = result.stdout
    if not raw:
        return ()
    if not raw.endswith("\0"):
        raise GuardrailError("Repository Git filter configuration is malformed")
    keys = raw[:-1].split("\0")
    if len(keys) > MAX_GIT_FILTER_CONFIG_KEYS:
        raise GuardrailError(
            "Repository Git filter configuration exceeds the "
            f"{MAX_GIT_FILTER_CONFIG_KEYS}-key limit"
        )
    prefixes: set[str] = set()
    for key in keys:
        try:
            encoded = key.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise GuardrailError(
                "Repository Git filter configuration is not UTF-8"
            ) from exc
        if (
            not key
            or len(encoded) > MAX_GIT_FILTER_KEY_BYTES
            or any(ord(character) < 32 or ord(character) == 127 for character in key)
        ):
            raise GuardrailError(
                "Repository Git filter configuration exceeds its key limit"
            )
        folded = key.casefold()
        suffix = next(
            (
                candidate
                for candidate in _FILTER_COMMAND_SUFFIXES
                if folded.endswith("." + candidate)
            ),
            None,
        )
        if suffix is None or not folded.startswith("filter."):
            raise GuardrailError("Repository Git filter configuration is malformed")
        prefix = key[: -(len(suffix) + 1)]
        if prefix.casefold() == "filter.":
            raise GuardrailError("Repository Git filter configuration is malformed")
        prefixes.add(prefix)
        if len(prefixes) > MAX_GIT_FILTER_DRIVERS:
            raise GuardrailError(
                "Repository Git filter configuration exceeds the "
                f"{MAX_GIT_FILTER_DRIVERS}-driver limit"
            )

    overrides = []
    override_bytes = 0
    for prefix in sorted(prefixes, key=lambda value: (value.casefold(), value)):
        driver_overrides = (
            (f"{prefix}.clean", ""),
            (f"{prefix}.smudge", ""),
            (f"{prefix}.process", ""),
            (f"{prefix}.required", "false"),
        )
        override_bytes += sum(
            len(key.encode("utf-8")) + len(value)
            for key, value in driver_overrides
        )
        if override_bytes > MAX_GIT_FILTER_OVERRIDE_BYTES:
            raise GuardrailError(
                "Repository Git filter overrides exceed the "
                f"{MAX_GIT_FILTER_OVERRIDE_BYTES}-byte environment limit"
            )
        overrides.extend(driver_overrides)
    return tuple(overrides)


def _git_subprocess_env(repo_root: Optional[Path] = None) -> Dict[str, str]:
    """Return a non-interactive, local-object-only Git environment.

    System and global config are disabled, so an exact repository path and a
    narrow inert worktree-semantics allowlist are supplied as process-local
    values when known. This preserves bind-mounted container and CRLF/case/
    symlink behavior without trusting a wildcard or executable ambient config.
    """
    environment = os.environ.copy()
    for name in tuple(environment):
        canonical = name.upper()
        if canonical in _GIT_AMBIENT_OVERRIDE_NAMES or canonical.startswith(
            _GIT_AMBIENT_OVERRIDE_PREFIXES
        ):
            environment.pop(name, None)
    environment.update(
        {
            "GCM_INTERACTIVE": "never",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "5",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": os.devnull,
            "GIT_CONFIG_KEY_1": "core.fsmonitor",
            "GIT_CONFIG_VALUE_1": "false",
            "GIT_CONFIG_KEY_2": "diff.ignoreSubmodules",
            "GIT_CONFIG_VALUE_2": "dirty",
            "GIT_CONFIG_KEY_3": "status.submoduleSummary",
            "GIT_CONFIG_VALUE_3": "false",
            "GIT_CONFIG_KEY_4": "submodule.recurse",
            "GIT_CONFIG_VALUE_4": "false",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_TRACE2": "0",
            "GIT_TRACE2_EVENT": "0",
            "GIT_TRACE2_PERF": "0",
        }
    )
    if repo_root is not None:
        resolved = str(repo_root.resolve(strict=False))
        process_values = [
            ("safe.directory", resolved),
            *_ambient_worktree_config_overrides(resolved),
            *_repository_filter_config_overrides(resolved),
        ]
        environment["GIT_CONFIG_COUNT"] = str(5 + len(process_values))
        for offset, (key, value) in enumerate(process_values, start=5):
            environment[f"GIT_CONFIG_KEY_{offset}"] = key
            environment[f"GIT_CONFIG_VALUE_{offset}"] = value
    return environment


def _trusted_git_executable(repo_root: Path) -> str:
    """Resolve Git outside the inspected repository to prevent shadowing."""
    raw = shutil.which("git")
    if raw is None:
        raise FileNotFoundError("git is required")
    try:
        repository = repo_root.resolve()
        selected = Path(os.path.abspath(raw))
        selected.relative_to(repository)
    except ValueError:
        pass
    except OSError as exc:
        raise ValueError("Cannot resolve the Git executable safely") from exc
    else:
        raise ValueError(
            "Refusing to execute a Git binary from inside the inspected repository"
        )
    try:
        executable = selected.resolve(strict=True)
        executable.relative_to(repository)
    except ValueError:
        pass
    except OSError as exc:
        raise ValueError("Cannot resolve the Git executable safely") from exc
    else:
        raise ValueError(
            "Refusing to execute a Git binary from inside the inspected repository"
        )
    try:
        metadata = executable.stat()
    except OSError as exc:
        raise ValueError("Cannot inspect the Git executable safely") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Git executable is not a regular file")
    return str(executable)


def _git_command(repo_root: Path, *arguments: str) -> List[str]:
    worktree = repo_root.resolve(strict=False)
    return [
        _trusted_git_executable(worktree),
        "-C",
        str(worktree),
        f"--work-tree={worktree}",
        *arguments,
    ]


def _git_run(
    repo_root: Path,
    args: List[str],
    *,
    environment: Optional[Mapping[str, str]] = None,
    deadline_seconds: Optional[float] = None,
) -> subprocess.CompletedProcess:
    """Run git against a specific repository root regardless of process CWD."""
    command = _offline_git_command(repo_root, args)
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_offline_git_environment(repo_root, environment),
    )
    _require_git_process_pipes(proc, "stdout", "stderr")
    deadline = _GitProcessDeadline(proc, deadline_seconds)
    deadline.start()

    captured: Dict[str, bytes] = {}
    overflows: List[str] = []
    read_errors: List[BaseException] = []
    state_lock = threading.Lock()

    def terminate() -> None:
        try:
            proc.kill()
        except OSError:
            # The child may have exited between the bounded read and kill.
            pass

    def read_stream(stream: BinaryIO, name: str, limit: int) -> None:
        data = bytearray()
        try:
            while True:
                remaining = limit - len(data)
                chunk = stream.read(min(_GIT_STREAM_CHUNK_BYTES, remaining + 1))
                if not chunk:
                    break
                if len(chunk) > remaining:
                    with state_lock:
                        overflows.append(name)
                    terminate()
                    break
                data.extend(chunk)
        except BaseException as exc:
            with state_lock:
                read_errors.append(exc)
            terminate()
        finally:
            captured[name] = bytes(data)
            stream.close()

    readers = [
        threading.Thread(
            target=read_stream,
            args=(proc.stdout, "stdout", MAX_GIT_COMMAND_OUTPUT_BYTES),
            daemon=True,
        ),
        threading.Thread(
            target=read_stream,
            args=(proc.stderr, "stderr", MAX_GIT_DIAGNOSTIC_BYTES),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()
    try:
        returncode = proc.wait()
    except BaseException:
        terminate()
        raise
    finally:
        for reader in readers:
            reader.join()
        deadline.cancel()

    deadline.raise_if_expired()
    if read_errors:
        raise read_errors[0]
    if overflows:
        name = overflows[0]
        limit = (
            MAX_GIT_COMMAND_OUTPUT_BYTES
            if name == "stdout"
            else MAX_GIT_DIAGNOSTIC_BYTES
        )
        raise GuardrailError(
            f"Git command {name} exceeds the {limit}-byte limit"
        )

    def as_text(data: bytes) -> str:
        return _decode_git_text(data, successful=returncode == 0)

    stdout = as_text(captured["stdout"])
    stderr = as_text(captured["stderr"])
    result = subprocess.CompletedProcess(command, returncode, stdout, stderr)
    if returncode != 0:
        raise subprocess.CalledProcessError(
            returncode,
            command,
            output=stdout,
            stderr=stderr,
        )
    return result


def _iter_git_nul_records(
    repo_root: Path,
    args: List[str],
    *,
    max_records: Optional[int] = None,
) -> Iterator[bytes]:
    """Stream bounded NUL-delimited records from Git.

    Git filename commands can emit output proportional to the repository.  A
    normal ``capture_output`` call would allocate all of it before boundver had
    an opportunity to enforce its entry/path limits.  This parser keeps only a
    fixed-size read chunk plus the current bounded record in memory.
    """
    command = _offline_git_command(repo_root, args)
    record_limit = MAX_GIT_TREE_ENTRIES if max_records is None else max_records
    if record_limit < 0:
        raise ValueError("Git listing record limit must be non-negative")
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_offline_git_environment(repo_root),
    )
    _require_git_process_pipes(proc, "stdout", "stderr")
    diagnostic = _BoundedGitDiagnosticDrain(proc)
    deadline = _GitProcessDeadline(proc)
    deadline.start()
    pending = bytearray()
    output_bytes = 0
    record_count = 0
    try:
        while True:
            chunk = proc.stdout.read(_GIT_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            output_bytes += len(chunk)
            if output_bytes > MAX_GIT_LIST_OUTPUT_BYTES:
                raise GuardrailError(
                    "Git listing exceeds the "
                    f"{MAX_GIT_LIST_OUTPUT_BYTES}-byte transport limit"
                )
            pending.extend(chunk)
            consumed = 0
            while True:
                terminator = pending.find(b"\0", consumed)
                if terminator < 0:
                    break
                record = bytes(pending[consumed:terminator])
                consumed = terminator + 1
                if not record:
                    continue
                record_count += 1
                if record_count > record_limit:
                    raise GuardrailError(
                        "Git listing exceeds the "
                        f"{record_limit}-entry limit"
                    )
                if len(record) > MAX_GIT_LIST_RECORD_BYTES:
                    raise GuardrailError(
                        "Git listing record exceeds the "
                        f"{MAX_GIT_LIST_RECORD_BYTES}-byte limit"
                    )
                yield record
            if consumed:
                del pending[:consumed]
            if len(pending) > MAX_GIT_LIST_RECORD_BYTES:
                raise GuardrailError(
                    "Git listing record exceeds the "
                    f"{MAX_GIT_LIST_RECORD_BYTES}-byte limit"
                )

        returncode = proc.wait()
        deadline.raise_if_expired()
        stderr = diagnostic.finish()
        if returncode != 0:
            raise subprocess.CalledProcessError(
                returncode,
                command,
                stderr=stderr,
            )
        if pending:
            raise ValueError("Truncated NUL-delimited Git listing output")
    except BaseException as exc:
        if deadline.expired.is_set():
            try:
                deadline.raise_if_expired()
            except GuardrailError as timeout_error:
                raise timeout_error from exc
        raise
    finally:
        active_error = sys.exc_info()[0] is not None
        deadline.cancel()
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        try:
            diagnostic.finish()
        except BaseException:
            if not active_error:
                raise


def _iter_bounded_git_paths(
    repo_root: Path,
    args: List[str],
) -> Iterator[str]:
    """Yield filename-safe Git paths with per-path and aggregate ceilings."""
    total_path_bytes = 0
    for raw_path in _iter_git_nul_records(repo_root, args):
        if len(raw_path) > MAX_GIT_PATH_BYTES:
            raise GuardrailError(
                "Git path exceeds the "
                f"{MAX_GIT_PATH_BYTES}-byte limit"
            )
        total_path_bytes += len(raw_path)
        if total_path_bytes > MAX_GIT_TOTAL_PATH_BYTES:
            raise GuardrailError(
                "Git paths exceed the "
                f"{MAX_GIT_TOTAL_PATH_BYTES}-byte aggregate limit"
            )
        yield os.fsdecode(raw_path)


def _resolve_head_oid(repo_root: Path) -> Optional[str]:
    """Resolve HEAD once, returning ``None`` for an unborn repository."""
    try:
        result = _git_run(
            repo_root,
            ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
        )
    except subprocess.CalledProcessError as exc:
        if (
            exc.returncode == 1
            and _git_error_stream_is_empty(exc.stdout)
            and _git_error_stream_is_empty(exc.stderr)
        ):
            try:
                if _head_is_provably_unborn(repo_root):
                    return None
            except ValueError as classification_error:
                raise ValueError(
                    "Cannot resolve HEAD commit: git rev-parse returned no "
                    "commit and the unborn-HEAD check failed: "
                    f"{classification_error}"
                ) from classification_error
            try:
                diagnostic_result = _git_run(
                    repo_root,
                    ["rev-parse", "--verify", "HEAD^{commit}"],
                )
            except (subprocess.CalledProcessError, OSError) as diagnostic_error:
                raise ValueError(
                    "Cannot resolve HEAD commit: HEAD is not unborn and git "
                    "rev-parse could not peel it to a commit "
                    f"({_git_failure_detail(diagnostic_error)})"
                ) from diagnostic_error
            return _validated_git_object_id(
                diagnostic_result.stdout,
                "git rev-parse",
            )
        raise ValueError(
            "Cannot resolve HEAD commit: git rev-parse failed "
            f"({_git_failure_detail(exc)})"
        ) from exc
    except OSError as exc:
        raise ValueError(
            "Cannot resolve HEAD commit: git rev-parse failed "
            f"({_git_failure_detail(exc)})"
        ) from exc
    return _validated_git_object_id(result.stdout, "git rev-parse")


def _is_git_repository(repo_root: Path) -> bool:
    """Return whether *repo_root* is inside a readable Git work tree.

    This deliberately distinguishes a directory with no repository (where the
    documented working-tree filesystem fallback is useful) from a real
    repository whose index cannot be snapshotted.  The latter includes
    unresolved merge stages and must fail closed.
    """
    try:
        result = _git_run(repo_root, ["rev-parse", "--is-inside-work-tree"])
    except (subprocess.CalledProcessError, OSError):
        # Preserve the documented non-Git fallback, but never reinterpret an
        # operational failure in an established worktree as "not a repo".
        # Linked worktrees use a .git file; ordinary worktrees use a directory.
        try:
            (repo_root / ".git").lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True
    return result.stdout.strip().lower() == "true"


def _parse_ls_tree_record(record: bytes) -> tuple[GitTreeEntry, int]:
    """Parse one filename-safe ``git ls-tree -z`` record."""
    try:
        header, raw_path = record.split(b"\t", 1)
        raw_mode, raw_type, raw_oid = header.split(b" ", 2)
        mode = raw_mode.decode("ascii")
        object_type = raw_type.decode("ascii")
        oid = raw_oid.decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"Malformed git ls-tree record: {record!r}") from exc
    if not raw_path:
        raise ValueError("Malformed git ls-tree record with an empty path")
    if len(raw_path) > MAX_GIT_PATH_BYTES:
        raise GuardrailError(
            "Git path exceeds the "
            f"{MAX_GIT_PATH_BYTES}-byte limit"
        )
    if len(mode) != 6 or not mode.isdigit():
        raise ValueError(f"Malformed Git mode {mode!r} for {os.fsdecode(raw_path)!r}")
    if object_type not in {"blob", "commit"}:
        raise ValueError(
            f"Unsupported Git object type {object_type!r} for "
            f"{os.fsdecode(raw_path)!r}"
        )
    if len(oid) not in {40, 64} or any(
        character not in "0123456789abcdefABCDEF" for character in oid
    ):
        raise ValueError(
            f"Malformed Git object ID {oid!r} for {os.fsdecode(raw_path)!r}"
        )
    path = os.fsdecode(raw_path)
    return GitTreeEntry(path, mode, object_type, oid), len(raw_path)


def _collect_ls_tree_entries(records: Iterator[bytes]) -> Dict[str, GitTreeEntry]:
    """Collect a streamed tree within the snapshot's hard path ceilings."""
    entries: Dict[str, GitTreeEntry] = {}
    total_path_bytes = 0
    for record in records:
        if len(entries) >= MAX_GIT_TREE_ENTRIES:
            raise GuardrailError(
                "Git listing exceeds the "
                f"{MAX_GIT_TREE_ENTRIES}-entry limit"
            )
        entry, path_bytes = _parse_ls_tree_record(record)
        total_path_bytes += path_bytes
        if total_path_bytes > MAX_GIT_TOTAL_PATH_BYTES:
            raise GuardrailError(
                "Git tree paths exceed the "
                f"{MAX_GIT_TOTAL_PATH_BYTES}-byte aggregate limit"
            )
        if entry.path in entries:
            raise ValueError(
                f"Duplicate path in captured Git tree: {entry.path!r}"
            )
        entries[entry.path] = entry
    return entries


def _capture_tree_entries(
    repo_root: Path,
    treeish: str,
    source_label: str,
) -> Dict[str, GitTreeEntry]:
    """Capture one immutable tree without consulting worktree conversion hooks."""
    try:
        return _collect_ls_tree_entries(
            _iter_git_nul_records(
                repo_root,
                ["ls-tree", "-r", "-z", "--full-tree", treeish],
            )
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise ValueError(
            f"Cannot enumerate captured {source_label} tree {treeish}: "
            f"git ls-tree failed ({_git_failure_detail(exc)})"
        ) from exc


def _capture_index_membership(repo_root: Path) -> _IndexMembership:
    """Capture bounded index membership and skip-worktree state together."""
    try:
        records = _iter_git_nul_records(
            repo_root,
            [
                "--literal-pathspecs",
                "ls-files",
                "--cached",
                "-t",
                "-z",
                "--",
            ],
        )
        tracked_paths = set()
        skip_worktree_paths = set()
        total_path_bytes = 0
        for record in records:
            if len(record) < 3 or record[1:2] != b" ":
                raise ValueError("Malformed tagged git ls-files record")
            tag = record[:1]
            if tag not in {b"H", b"S"}:
                raise ValueError(
                    f"Unsupported git ls-files index status tag {tag!r}"
                )
            raw_path = record[2:]
            if len(raw_path) > MAX_GIT_PATH_BYTES:
                raise GuardrailError(
                    "Git path exceeds the "
                    f"{MAX_GIT_PATH_BYTES}-byte limit"
                )
            total_path_bytes += len(raw_path)
            if total_path_bytes > MAX_GIT_TOTAL_PATH_BYTES:
                raise GuardrailError(
                    "Git paths exceed the "
                    f"{MAX_GIT_TOTAL_PATH_BYTES}-byte aggregate limit"
                )
            if len(tracked_paths) >= MAX_GIT_TREE_ENTRIES:
                raise GuardrailError(
                    "Git index membership exceeds the "
                    f"{MAX_GIT_TREE_ENTRIES}-entry limit"
                )
            path = os.fsdecode(raw_path)
            if path in tracked_paths:
                raise ValueError("Duplicate path in captured Git index membership")
            tracked_paths.add(path)
            if tag == b"S":
                skip_worktree_paths.add(path)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise ValueError(
            "Cannot capture index path membership: git ls-files failed "
            f"({_git_failure_detail(exc)})"
        ) from exc
    return _IndexMembership(
        tracked_paths=frozenset(tracked_paths),
        skip_worktree_paths=frozenset(skip_worktree_paths),
    )


def _capture_git_source_snapshot(repo_root: Path, source: str) -> GitSourceSnapshot:
    """Capture one immutable tree for a ``head`` or ``index`` operation."""
    if source not in {"head", "index"}:
        raise ValueError(f"Cannot capture a Git snapshot for source {source!r}")

    head_oid = _resolve_head_oid(repo_root)
    tracked_paths: FrozenSet[str] = frozenset()
    skip_worktree_paths: FrozenSet[str] = frozenset()
    if source == "head":
        if head_oid is None:
            raise ValueError("HEAD does not resolve to a commit")
        treeish = head_oid
    else:
        # ``write-tree`` validates that the index can be represented by one
        # complete tree (and fails closed for unresolved merge stages).  It
        # writes only an immutable object; it does not mutate index entries or
        # the working tree.
        try:
            result = _git_run(repo_root, ["write-tree"])
        except (subprocess.CalledProcessError, OSError) as exc:
            raise ValueError(
                "Cannot capture index as a complete Git tree: "
                f"git write-tree failed ({_git_failure_detail(exc)})"
            ) from exc
        treeish = _validated_git_object_id(result.stdout, "git write-tree")

    entries = _capture_tree_entries(repo_root, treeish, source)
    if source == "index":
        # A written tree omits CE_INTENT_TO_ADD entries. Require both the
        # written tree and the bounded index name set to remain stable so the
        # immutable content tree and the additional tracking membership
        # describe one coherent index state.
        membership = _capture_index_membership(repo_root)
        try:
            confirmation = _git_run(repo_root, ["write-tree"])
        except (subprocess.CalledProcessError, OSError) as exc:
            raise ValueError(
                "Cannot confirm captured index tree: git write-tree failed "
                f"({_git_failure_detail(exc)})"
            ) from exc
        confirmed_treeish = _validated_git_object_id(
            confirmation.stdout,
            "git write-tree confirmation",
        )
        if confirmed_treeish != treeish:
            raise ValueError("Index changed while capturing tracked paths; retry")
        confirmed_membership = _capture_index_membership(repo_root)
        if confirmed_membership != membership:
            raise ValueError(
                "Index path membership changed while capturing tracked paths; retry"
            )
        tracked_paths = membership.tracked_paths
        if set(entries) - tracked_paths:
            raise ValueError(
                "Captured index membership omitted materialized tree paths"
            )
        if not membership.skip_worktree_paths <= set(entries):
            raise ValueError(
                "Captured skip-worktree membership omitted materialized tree paths"
            )
        skip_worktree_paths = membership.skip_worktree_paths
    try:
        filemode_result = _git_run(repo_root, ["config", "--bool", "core.filemode"])
    except subprocess.CalledProcessError as exc:
        if (
            exc.returncode == 1
            and _git_error_stream_is_empty(exc.stdout)
            and _git_error_stream_is_empty(exc.stderr)
        ):
            # An unset value has Git's documented true default.
            core_filemode = True
        else:
            raise ValueError(
                "Cannot read Git core.filemode: git config failed "
                f"({_git_failure_detail(exc)})"
            ) from exc
    except OSError as exc:
        raise ValueError(
            "Cannot read Git core.filemode: git config failed "
            f"({_git_failure_detail(exc)})"
        ) from exc
    else:
        core_filemode = filemode_result.stdout.strip().lower() != "false"
    return GitSourceSnapshot(
        source=source,
        tree_oid=treeish,
        entries=entries,
        head_oid=head_oid,
        filemode=core_filemode,
        tracked_paths=tracked_paths,
        skip_worktree_paths=skip_worktree_paths,
    )


def _validated_revision(value: str, label: str) -> str:
    """Return one caller-supplied Git revision expression safe for argv use."""
    if not isinstance(value, str):
        raise ValueError(f"{label} Git ref must be text")
    if (
        not value
        or value != value.strip()
        or value.startswith("-")
        or len(value) > MAX_GIT_FAILURE_DETAIL_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(
            f"Invalid {label} Git ref: "
            f"{_bounded_diagnostic_repr(value, max_chars=256)}"
        )
    return value


def _resolve_git_commit(repo_root: Path, ref: str, *, label: str = "endpoint") -> str:
    """Resolve an unambiguous revision expression to exactly one commit ID."""
    revision = _validated_revision(ref, label)
    try:
        result = _git_run(
            repo_root,
            [
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{revision}^{{commit}}",
            ],
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(
            f"Cannot resolve {label} Git ref {revision!r} to one commit "
            f"({_git_failure_detail(exc)})"
        ) from exc
    if result.stderr.strip():
        raise ValueError(
            f"Cannot resolve {label} Git ref {revision!r} unambiguously: "
            f"git rev-parse emitted {_bounded_diagnostic_repr(result.stderr.strip())}"
        )
    return _validated_git_object_id(result.stdout, f"git rev-parse for {label}")


def _capture_git_ref_snapshot(
    repo_root: Path,
    ref: str,
    *,
    label: str = "endpoint",
) -> GitSourceSnapshot:
    """Capture the immutable tree reached by an arbitrary explicit Git ref.

    The returned snapshot uses the existing ``head`` reader contract because
    its entries are commit-backed, but ``head_oid`` records the resolved
    endpoint rather than consulting the moving ``HEAD`` ref.
    """
    commit_oid = _resolve_git_commit(repo_root, ref, label=label)
    try:
        tree_result = _git_run(
            repo_root,
            [
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{commit_oid}^{{tree}}",
            ],
        )
        tree_oid = _validated_git_object_id(
            tree_result.stdout,
            f"git rev-parse tree for {label}",
        )
        entries = _collect_ls_tree_entries(
            _iter_git_nul_records(
                repo_root,
                ["ls-tree", "-r", "-z", "--full-tree", tree_oid],
            )
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(
            f"Cannot capture {label} Git tree for {ref!r} "
            f"({_git_failure_detail(exc)})"
        ) from exc

    try:
        filemode_result = _git_run(repo_root, ["config", "--bool", "core.filemode"])
    except subprocess.CalledProcessError as exc:
        if (
            exc.returncode == 1
            and _git_error_stream_is_empty(exc.stdout)
            and _git_error_stream_is_empty(exc.stderr)
        ):
            core_filemode = True
        else:
            raise ValueError(
                "Cannot read Git core.filemode: git config failed "
                f"({_git_failure_detail(exc)})"
            ) from exc
    except OSError as exc:
        raise ValueError(
            "Cannot read Git core.filemode: git config failed "
            f"({_git_failure_detail(exc)})"
        ) from exc
    else:
        core_filemode = filemode_result.stdout.strip().lower() != "false"

    return GitSourceSnapshot(
        source="head",
        tree_oid=tree_oid,
        entries=entries,
        head_oid=commit_oid,
        filemode=core_filemode,
    )


def _git_merge_base(repo_root: Path, left_oid: str, right_oid: str) -> str:
    """Return the unique merge base of two already validated commit IDs."""
    left = _validated_git_object_id(left_oid, "left merge-base endpoint")
    right = _validated_git_object_id(right_oid, "right merge-base endpoint")
    try:
        result = _git_run(repo_root, ["merge-base", "--all", left, right])
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(
            "Cannot determine a merge base from the available Git history "
            f"({_git_failure_detail(exc)})"
        ) from exc
    candidates = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(candidates) != 1:
        raise ValueError(
            "Range review requires exactly one merge base; Git returned "
            f"{len(candidates)} candidates"
        )
    return _validated_git_object_id(candidates[0], "git merge-base")


def _git_repository_is_shallow(repo_root: Path) -> bool:
    """Return Git's explicit shallow-repository state or fail closed."""
    try:
        result = _git_run(repo_root, ["rev-parse", "--is-shallow-repository"])
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(
            "Cannot determine whether Git history is shallow "
            f"({_git_failure_detail(exc)})"
        ) from exc
    value = result.stdout.strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(
            "git rev-parse --is-shallow-repository returned an invalid value: "
            f"{_bounded_diagnostic_repr(value, max_chars=64)}"
        )
    return value == "true"


def _snapshot_files(snapshot: GitSourceSnapshot, path: str) -> List[str]:
    """List files at/below one literal repo path in a captured tree."""
    normalized = Path(path).as_posix().strip("/")
    if normalized in {"", "."}:
        return sorted(snapshot.entries)
    prefix = normalized + "/"
    return sorted(
        candidate
        for candidate in snapshot.entries
        if candidate == normalized or candidate.startswith(prefix)
    )


def _snapshot_tracked_files(snapshot: GitSourceSnapshot, path: str) -> List[str]:
    """List tracked paths, including intent-to-add index membership."""
    normalized = Path(path).as_posix().strip("/")
    if normalized in {"", "."}:
        return sorted(snapshot.tracked_paths)
    prefix = normalized + "/"
    return sorted(
        candidate
        for candidate in snapshot.tracked_paths
        if candidate == normalized or candidate.startswith(prefix)
    )


def _working_tree_mode(
    repo_root: Path,
    repo_rel: str,
    tracked_entry: Optional[GitTreeEntry] = None,
    *,
    core_filemode: bool = True,
    path_stat: Optional[os.stat_result] = None,
) -> tuple:
    """Return canonical Git mode/type for one working-tree identity sample."""
    if path_stat is None:
        full_path = repo_root / repo_rel
        try:
            path_stat = full_path.lstat()
        except FileNotFoundError as exc:
            raise ValueError(f"File disappeared while hashing: {repo_rel}") from exc
    if stat.S_ISLNK(path_stat.st_mode):
        return "120000", "blob"
    if stat.S_ISREG(path_stat.st_mode):
        if (
            tracked_entry is not None
            and tracked_entry.mode in {"100644", "100755"}
            and not core_filemode
        ):
            return tracked_entry.mode, "blob"
        executable = bool(
            path_stat.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        )
        return ("100755" if executable else "100644"), "blob"
    raise ValueError(f"Unsupported working-tree file type at {repo_rel}")


def _git_cat_blob(
    repo_root: Path,
    ref: str,
    *,
    max_bytes: int = MAX_GIT_BLOB_BYTES,
) -> bytes:
    """Read a single git blob at the given ref as raw bytes (no text-mode CRLF conversion)."""
    if max_bytes < 0:
        raise ValueError("Git blob byte limit must be non-negative")
    effective_limit = min(max_bytes, MAX_GIT_BLOB_BYTES)
    command = _offline_git_command(repo_root, ["cat-file", "blob", ref])
    # ``capture_output`` would buffer the entire object before the size check.
    # Bound the read itself and terminate Git as soon as the limit is crossed.
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_offline_git_environment(repo_root),
    )
    _require_git_process_pipes(proc, "stdout", "stderr")
    diagnostic = _BoundedGitDiagnosticDrain(proc)
    deadline = _GitProcessDeadline(proc)
    deadline.start()
    try:
        content = proc.stdout.read(effective_limit + 1)
        deadline.raise_if_expired()
        if len(content) > effective_limit:
            proc.kill()
            proc.wait()
            raise GuardrailError(
                f"Hash guardrail exceeded: Git blob too large "
                f"(>{effective_limit} bytes) for ref {ref!r}"
            )
        returncode = proc.wait()
        deadline.raise_if_expired()
        stderr = diagnostic.finish()
        if returncode != 0:
            raise subprocess.CalledProcessError(
                returncode,
                command,
                output=content,
                stderr=stderr,
            )
        return content
    except BaseException as exc:
        if deadline.expired.is_set():
            try:
                deadline.raise_if_expired()
            except GuardrailError as timeout_error:
                raise timeout_error from exc
        raise
    finally:
        active_error = sys.exc_info()[0] is not None
        deadline.cancel()
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        try:
            diagnostic.finish()
        except BaseException:
            if not active_error:
                raise


def _parse_batch_header(header_bytes: bytes, ref: str) -> int:
    """Validate one ``cat-file --batch`` header and return its blob size."""
    try:
        header = header_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"Malformed non-ASCII git cat-file header for {ref!r}"
        ) from exc
    if header.endswith(" missing"):
        raise ValueError(f"Git blob not found for ref {ref!r}")
    last_space = header.rfind(" ")
    type_space = header.rfind(" ", 0, last_space)
    if last_space < 0 or type_space < 0:
        raise ValueError(
            f"Malformed git cat-file header for {ref!r}: {header!r}"
        )
    object_type = header[type_space + 1:last_space]
    if object_type != "blob":
        raise ValueError(
            f"Expected a Git blob for {ref!r}, got {object_type!r}"
        )
    try:
        size = int(header[last_space + 1:])
    except ValueError as exc:
        raise ValueError(
            f"Malformed git cat-file size for {ref!r}: {header!r}"
        ) from exc
    if size < 0:
        raise ValueError(f"Negative git blob size for {ref!r}: {size}")
    if size > MAX_GIT_BLOB_BYTES:
        raise GuardrailError(
            f"Hash guardrail exceeded: Git blob too large "
            f"({size} bytes) for ref {ref!r}"
        )
    return size


class _GitBlobSession:
    """Read immutable Git blobs through one bounded ``cat-file`` process.

    The session is deliberately operation-scoped: it shares subprocess
    transport, not content or authority, between reads from one captured Git
    snapshot.  Every request remains caller-bounded and accepts only a full
    object ID, so neither paths nor revision expressions enter the line-based
    batch protocol.
    """

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.command = _offline_git_command(
            repo_root, ["cat-file", "--batch"]
        )
        self._proc: Optional[subprocess.Popen] = None
        self._stderr_drain: Optional[_BoundedGitDiagnosticDrain] = None
        self._deadline: Optional[_GitProcessDeadline] = None
        self._closed = False
        self._lock = threading.RLock()

    def _start(self) -> subprocess.Popen:
        if self._closed:
            raise ValueError("Git blob session is closed")
        if self._proc is not None:
            return self._proc
        command = _offline_git_command(
            self.repo_root, ["cat-file", "--batch"]
        )
        diagnostic: Optional[_BoundedGitDiagnosticDrain] = None
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_offline_git_environment(self.repo_root),
            )
            _require_git_process_pipes(proc, "stdin", "stdout", "stderr")
            diagnostic = _BoundedGitDiagnosticDrain(proc)
            deadline = _GitProcessDeadline(proc)
            deadline.start()
        except BaseException:
            if diagnostic is not None:
                try:
                    diagnostic.finish()
                except BaseException:
                    pass
            raise
        self._proc = proc
        self._stderr_drain = diagnostic
        self._deadline = deadline
        return proc

    def _process_error(self, returncode: int) -> subprocess.CalledProcessError:
        stderr = (
            self._stderr_drain.snapshot()
            if self._stderr_drain is not None
            else b""
        )
        return subprocess.CalledProcessError(
            returncode,
            self.command,
            stderr=stderr,
        )

    def _release_transport(self, *, kill: bool) -> Optional[int]:
        proc = self._proc
        diagnostic = self._stderr_drain
        deadline = self._deadline
        self._proc = None
        self._stderr_drain = None
        self._deadline = None
        if proc is None:
            if deadline is not None:
                deadline.cancel()
            if diagnostic is not None:
                diagnostic.finish()
            return None
        try:
            if kill and proc.poll() is None:
                proc.kill()
            try:
                returncode = proc.wait()
            except BaseException:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                raise
            return returncode
        finally:
            if deadline is not None:
                deadline.cancel()
            if proc.stdin is not None and not proc.stdin.closed:
                try:
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            if proc.stdout is not None:
                proc.stdout.close()
            if diagnostic is not None:
                diagnostic.finish()

    def _abort_transport(self) -> None:
        self._release_transport(kill=True)

    def read_blob(
        self,
        oid: str,
        *,
        max_bytes: int = MAX_GIT_BLOB_BYTES,
    ) -> bytes:
        """Return one blob while enforcing the caller's pre-read byte limit."""
        with self._lock:
            return self._read_blob_locked(oid, max_bytes=max_bytes)

    def _read_blob_locked(self, oid: str, *, max_bytes: int) -> bytes:
        if max_bytes < 0:
            raise ValueError("Git blob byte limit must be non-negative")
        object_id = _validated_git_object_id(oid, "Git blob request")
        effective_limit = min(max_bytes, MAX_GIT_BLOB_BYTES)
        proc = self._start()
        deadline = self._deadline
        _require_git_process_pipes(proc, "stdin", "stdout")
        try:
            try:
                proc.stdin.write(object_id.encode("ascii") + b"\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                returncode = proc.poll()
                if returncode is None:
                    returncode = 1
                raise self._process_error(returncode) from exc

            header = proc.stdout.readline(MAX_GIT_BATCH_HEADER_BYTES + 1)
            if not header.endswith(b"\n"):
                if len(header) > MAX_GIT_BATCH_HEADER_BYTES:
                    raise ValueError(
                        f"Oversized git cat-file header for {object_id!r}"
                    )
                if not header:
                    returncode = proc.poll()
                    if returncode is None:
                        returncode = proc.wait()
                    if returncode != 0:
                        raise self._process_error(returncode)
                raise ValueError(
                    "Truncated git cat-file response before header for "
                    f"{object_id!r}"
                )
            size = _parse_batch_header(header[:-1], object_id)
            if size > effective_limit:
                raise GuardrailError(
                    f"Hash guardrail exceeded: Git blob too large "
                    f"({size} bytes) for ref {object_id!r}"
                )
            content = proc.stdout.read(size)
            if len(content) != size:
                raise ValueError(
                    f"Truncated git cat-file content for {object_id!r}: "
                    f"expected {size} bytes"
                )
            if proc.stdout.read(1) != b"\n":
                raise ValueError(
                    f"Malformed git cat-file terminator for {object_id!r}"
                )
            if deadline is not None:
                deadline.raise_if_expired()
            return content
        except BaseException as exc:
            # Any failed response leaves the line protocol potentially out of
            # sync. Reap it immediately; a later independent read may lazily
            # start a fresh bounded session.
            timed_out = deadline is not None and deadline.expired.is_set()
            self._abort_transport()
            if timed_out and deadline is not None:
                try:
                    deadline.raise_if_expired()
                except GuardrailError as timeout_error:
                    raise timeout_error from exc
            raise

    def close(self) -> None:
        """Finish the current batch and surface a late Git process failure."""
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is None:
            self._release_transport(kill=False)
            return
        _require_git_process_pipes(proc, "stdin")
        close_error: Optional[BaseException] = None
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError) as exc:
            close_error = exc
        try:
            returncode = proc.wait()
            if self._deadline is not None:
                self._deadline.raise_if_expired()
        except BaseException:
            self._abort_transport()
            raise
        process_error = self._process_error(returncode) if returncode != 0 else None
        self._release_transport(kill=False)
        if process_error is not None:
            raise process_error
        if close_error is not None:
            raise OSError("Could not close Git blob session input") from close_error

    def __enter__(self) -> "_GitBlobSession":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is None:
            self.close()
        else:
            with self._lock:
                self._closed = True
                self._abort_transport()

    def __del__(self) -> None:
        try:
            with self._lock:
                self._closed = True
                self._abort_transport()
        except Exception:
            pass


def _iter_git_blobs(
    repo_root: Path,
    refs: List[str],
    *,
    max_total_bytes: int = MAX_GIT_BATCH_BYTES,
    remaining_bytes: Optional[Callable[[], int]] = None,
) -> Iterator[Tuple[str, bytes]]:
    """Yield unique Git blobs under caller-specific and absolute ceilings."""
    if max_total_bytes < 0:
        raise ValueError("Git blob aggregate byte limit must be non-negative")
    effective_total_limit = min(
        max_total_bytes,
        MAX_GIT_REPOSITORY_SCAN_BYTES,
    )
    total_bytes = 0

    def remaining_limit() -> int:
        remaining = effective_total_limit - total_bytes
        if remaining_bytes is not None:
            logical_remaining = remaining_bytes()
            if logical_remaining < 0:
                raise GuardrailError(
                    "Hash guardrail exceeded: Git blobs exhausted the logical "
                    "aggregate byte limit"
                )
            remaining = min(remaining, logical_remaining)
        return remaining

    unique_refs = list(dict.fromkeys(refs))
    line_refs = [
        ref for ref in unique_refs if "\n" not in ref and "\r" not in ref
    ]
    line_ref_set = set(line_refs)
    for ref in unique_refs:
        if ref in line_ref_set:
            continue
        try:
            content = _git_cat_blob(
                repo_root,
                ref,
                max_bytes=remaining_limit(),
            )
        except subprocess.CalledProcessError as exc:
            raise ValueError(
                f"Git blob not found for ref containing a newline: {ref!r}"
            ) from exc
        total_bytes += len(content)
        yield ref, content

    if not line_refs:
        return

    command = _offline_git_command(repo_root, ["cat-file", "--batch"])
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_offline_git_environment(repo_root),
    )
    _require_git_process_pipes(proc, "stdin", "stdout", "stderr")
    diagnostic = _BoundedGitDiagnosticDrain(proc)
    deadline = _GitProcessDeadline(proc)
    deadline.start()
    try:
        for ref in line_refs:
            try:
                proc.stdin.write(os.fsencode(ref) + b"\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                returncode = proc.poll()
                if returncode is None:
                    returncode = proc.wait()
                raise subprocess.CalledProcessError(
                    returncode,
                    command,
                    stderr=diagnostic.snapshot(),
                ) from exc
            header = proc.stdout.readline(MAX_GIT_BATCH_HEADER_BYTES + 1)
            if not header.endswith(b"\n"):
                if not header:
                    returncode = proc.wait()
                    if returncode != 0:
                        raise subprocess.CalledProcessError(
                            returncode,
                            command,
                            stderr=diagnostic.finish(),
                        )
                if len(header) > MAX_GIT_BATCH_HEADER_BYTES:
                    raise ValueError(
                        f"Oversized git cat-file header for {ref!r}"
                    )
                raise ValueError(
                    f"Truncated git cat-file response before header for {ref!r}"
                )
            size = _parse_batch_header(header[:-1], ref)
            remaining = remaining_limit()
            if size > remaining:
                raise GuardrailError(
                    "Hash guardrail exceeded: Git blob size exceeds the "
                    f"{remaining}-byte remaining aggregate budget"
                )
            content = proc.stdout.read(size)
            if len(content) != size:
                raise ValueError(
                    f"Truncated git cat-file content for {ref!r}: "
                    f"expected {size} bytes"
                )
            if proc.stdout.read(1) != b"\n":
                raise ValueError(
                    f"Malformed git cat-file terminator for {ref!r}"
                )
            deadline.raise_if_expired()
            total_bytes += len(content)
            yield ref, content

        proc.stdin.close()
        returncode = proc.wait()
        deadline.raise_if_expired()
        stderr = diagnostic.finish()
        if returncode != 0:
            raise subprocess.CalledProcessError(
                returncode,
                command,
                stderr=stderr,
            )
    except BaseException as exc:
        if deadline.expired.is_set():
            try:
                deadline.raise_if_expired()
            except GuardrailError as timeout_error:
                raise timeout_error from exc
        raise
    finally:
        active_error = sys.exc_info()[0] is not None
        deadline.cancel()
        if not proc.stdin.closed:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        try:
            diagnostic.finish()
        except BaseException:
            if not active_error:
                raise


def _git_batch_cat(repo_root: Path, refs: List[str]) -> Dict[str, bytes]:
    """Batch-read multiple git objects via ``git cat-file --batch``.

    All objects are fetched in a single subprocess, replacing O(N) ``git show``
    calls with O(1).  Returns ``{ref: raw_bytes}``.  Missing, malformed,
    truncated, non-blob, and oversized responses raise instead of being
    confused with a valid empty blob.
    """
    blobs: Dict[str, bytes] = {}
    total_bytes = 0
    for ref, content in _iter_git_blobs(
        repo_root,
        refs,
        max_total_bytes=MAX_GIT_BATCH_BYTES,
    ):
        total_bytes += len(content)
        if total_bytes > MAX_GIT_BATCH_BYTES:
            raise GuardrailError(
                "Hash guardrail exceeded: Git batch blobs exceed the "
                f"{MAX_GIT_BATCH_BYTES}-byte aggregate limit"
            )
        blobs[ref] = content
    return blobs

def _to_posix(rel_path: str) -> str:
    # ``Path.as_posix`` converts native Windows separators while preserving a
    # literal backslash in a POSIX filename. A blind string replacement would
    # collapse two distinct Git paths on POSIX.
    return Path(rel_path).as_posix()


def _is_ignored(path: Path) -> bool:
    name = path.name
    return (
        name.startswith(".")
        or name == "__pycache__"
        or name == "node_modules"
        or name.endswith(".pyc")
        or name == "dist"
        or name == "build"
    )


def _load_gitignore_patterns(repo_root: Path) -> Optional["_GitignoreRules"]:
    """Parse .gitignore into a structured ruleset supporting negation and **."""
    gi = repo_root / ".gitignore"
    if not gi.exists():
        return None
    raw = _read_bounded_path_bytes(
        gi,
        ".gitignore",
        max_bytes=MAX_GITIGNORE_BYTES,
    )
    rules = _GitignoreRules()
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rules.add(line)
    return rules


class _GitignoreRules:
    """Structured gitignore rules supporting negation (!) and ** globs."""

    def __init__(self) -> None:
        # Preserve root anchoring separately from the normalized pattern;
        # otherwise stripping ``/`` would turn ``/foo`` into an any-depth rule.
        self._rules: List[tuple] = []  # (negate, pattern, anchored)
        self._has_negation = False
        self._match_steps = 0

    def add(self, raw_line: str) -> None:
        negate = raw_line.startswith("!")
        pattern = raw_line[1:] if negate else raw_line
        pattern = pattern.rstrip("/")
        anchored = pattern.startswith("/")
        if pattern:
            # A leading slash anchors a Git ignore rule at the repository root;
            # slash-containing rules are already root-relative here.
            pattern = pattern.lstrip("/")
        if pattern:
            try:
                pattern_bytes = len(pattern.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise GuardrailError(
                    "Gitignore guardrail exceeded: pattern contains invalid Unicode"
                ) from exc
            if pattern_bytes > MAX_GITIGNORE_PATTERN_BYTES:
                raise GuardrailError(
                    "Gitignore guardrail exceeded: pattern exceeds "
                    f"{MAX_GITIGNORE_PATTERN_BYTES} UTF-8 bytes"
                )
            try:
                _validate_glob_pattern_complexity(pattern)
            except ValueError as exc:
                raise GuardrailError(f"Gitignore guardrail exceeded: {exc}") from exc
            if len(self._rules) >= MAX_GITIGNORE_RULES:
                raise GuardrailError(
                    "Gitignore guardrail exceeded: more than "
                    f"{MAX_GITIGNORE_RULES} rules"
                )
            self._rules.append((negate, pattern, anchored))
            self._has_negation = self._has_negation or negate

    def _spend_match_steps(self, amount: int) -> None:
        if amount < 0 or amount > MAX_GITIGNORE_MATCH_STEPS - self._match_steps:
            raise GuardrailError(
                "Gitignore guardrail exceeded: more than "
                f"{MAX_GITIGNORE_MATCH_STEPS} aggregate matcher steps"
            )
        self._match_steps += amount

    def is_ignored(self, rel_path: str) -> bool:
        """Return True if rel_path should be excluded per gitignore rules."""
        rel_path = rel_path.replace("\\", "/")
        parts = rel_path.split("/")
        ignored = False
        for negate, pattern, anchored in self._rules:
            self._spend_match_steps(1)
            if self._matches(rel_path, parts, pattern, anchored=anchored):
                ignored = not negate
        return ignored

    def can_prune_directory(self, rel_path: str) -> bool:
        """Return whether an ignored directory is safe to skip entirely.

        A later negated rule may re-include a descendant, so retain traversal
        whenever any negation is present.  This is deliberately conservative:
        it preserves existing matching behavior while pruning the common
        all-exclusion case.
        """
        if self._has_negation:
            return False
        return self.is_ignored(rel_path)

    def _matches(
        self,
        rel_path: str,
        parts: List[str],
        pattern: str,
        *,
        anchored: bool,
    ) -> bool:
        # Pattern without / matches any path component
        if "/" not in pattern:
            candidate_parts = parts[:1] if anchored else parts
            for part in candidate_parts:
                self._spend_match_steps(1)
                if _match_text_glob(
                    part,
                    pattern,
                    _step_consumer=self._spend_match_steps,
                ):
                    return True
            return False
        # Git gives a trailing ``/**`` stricter semantics than the generic
        # recursive path glob: it matches everything *inside* the selected
        # directory, but not the directory entry (or a regular file with that
        # name) itself.  Requiring one final ordinary segment preserves that
        # rule while still allowing ``**`` to consume any deeper directories.
        match_pattern = pattern + "/*" if pattern.endswith("/**") else pattern
        # A matched directory also ignores its descendants.  Prefix acceptance
        # avoids repeated path slicing and nested recursive-wildcard regexes.
        return _match_path_glob(
            rel_path,
            match_pattern,
            _step_consumer=self._spend_match_steps,
            _allow_descendants=True,
        )


def _matches_gitignore(rel_path: str, patterns: "_GitignoreRules") -> bool:
    """Check if a repo-relative path matches gitignore rules."""
    return patterns.is_ignored(rel_path)


def list_head_files(repo_root: Path, path: str) -> List[str]:
    """List files at a repo-relative path as represented in HEAD."""
    try:
        files = list(
            _iter_bounded_git_paths(
                repo_root,
                [
                    "--literal-pathspecs",
                    "ls-tree",
                    "-r",
                    "-z",
                    "--name-only",
                    "HEAD",
                    "--",
                    path,
                ],
            )
        )
    except subprocess.CalledProcessError:
        return []
    if files:
        return files

    try:
        result = _git_run(repo_root, ["cat-file", "-t", f"HEAD:{path}"])
    except subprocess.CalledProcessError:
        return []
    return [_to_posix(path)] if result.stdout.strip() == "blob" else []


def _list_unborn_working_tree_paths(
    repo_root: Path,
    repo_rel_path: str,
) -> List[str]:
    """Return Git's bounded, non-ignored bootstrap corpus for an unborn tree."""
    bootstrap_args = [
        "--literal-pathspecs",
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        repo_rel_path,
    ]
    result_files = list(_iter_bounded_git_paths(repo_root, bootstrap_args))
    bootstrap_files: List[str] = []
    for path in result_files:
        full_path = repo_root / path
        try:
            path_stat = full_path.lstat()
        except FileNotFoundError:
            continue
        # Git represents an untracked embedded repository as one opaque
        # directory row (for example, ``svc/nested/``). Match superproject
        # semantics by excluding that row and never traversing the nested
        # repository. Ordinary files and symlinks remain hashable;
        # unsupported special files fail closed through the mode classifier.
        if stat.S_ISDIR(path_stat.st_mode):
            continue
        _working_tree_mode(
            repo_root,
            path,
            path_stat=path_stat,
        )
        bootstrap_files.append(path)
    return bootstrap_files


def _list_files_for_source(repo_root: Path, repo_rel_path: str, source: str) -> List[str]:
    if source == "head":
        return list_head_files(repo_root, repo_rel_path)
    # Index and working-tree use the same tracked file set.  The selected
    # source only controls whether content comes from the index or from disk.
    args = ["--literal-pathspecs", "ls-files", "--cached", "-z"]
    args.extend(["--", repo_rel_path])
    try:
        result_files = list(_iter_bounded_git_paths(repo_root, args))
    except subprocess.CalledProcessError:
        if source == "index":
            raise
        # Do not turn a Git failure inside a real repository into an
        # approximate filesystem fingerprint. Fallback is reserved for
        # non-Git/unborn first-run environments.
        try:
            _git_run(repo_root, ["rev-parse", "--git-dir"])
        except subprocess.CalledProcessError:
            git_failed = True
            result_files: List[str] = []
        else:
            raise
    else:
        git_failed = False

    # For working-tree source, exclude files that are tracked but deleted on disk.
    if result_files and source == "working-tree":
        result_files = [
            f
            for f in result_files
            if (repo_root / f).exists() or (repo_root / f).is_symlink()
        ]

    # An empty successful result is authoritative (for example, a legitimate
    # empty index). The one usability exception is an unborn repository in
    # working-tree mode: before the first commit there is no tracked-file view.
    # Ask the installed Git to enumerate non-ignored bootstrap candidates so
    # ignore behavior stays exact even across Git versions with different
    # wildmatch edge semantics. The bounded filesystem matcher is reserved for
    # directories that genuinely are not readable Git repositories.
    if not result_files and source == "working-tree" and not git_failed:
        head_oid = _resolve_head_oid(repo_root)
        if head_oid is None:
            return _list_unborn_working_tree_paths(repo_root, repo_rel_path)
    if not git_failed:
        return result_files

    # Fallback for non-git test/runtime environments: local filesystem enumeration.
    import sys as _sys
    print(
        "WARNING: git file listing failed; falling back to filesystem enumeration. "
        "Fingerprints may differ from git-based computation.",
        file=_sys.stderr,
    )
    target = repo_root / repo_rel_path
    if not target.exists():
        return []
    if target.is_file():
        return [_to_posix(str(target.relative_to(repo_root)))]
    gitignore_patterns = _load_gitignore_patterns(repo_root)
    target_rel = _to_posix(str(target.relative_to(repo_root)))
    if any(_is_ignored(Path(part)) for part in Path(target_rel).parts):
        return []
    if (
        gitignore_patterns is not None
        and gitignore_patterns.can_prune_directory(target_rel)
    ):
        return []

    def should_descend(directory: Path) -> bool:
        rel = _to_posix(str(directory.relative_to(repo_root)))
        if any(_is_ignored(Path(part)) for part in Path(rel).parts):
            return False
        return not (
            gitignore_patterns is not None
            and gitignore_patterns.can_prune_directory(rel)
        )

    def eligible_files() -> Iterator[Path]:
        for file_path in _iter_bounded_filesystem_paths(
            target,
            recursive=True,
            max_entries=MAX_FALLBACK_TRAVERSAL_ENTRIES,
            exceeded_message=(
                "Hash guardrail exceeded: filesystem traversal exceeds "
                f"{MAX_FALLBACK_TRAVERSAL_ENTRIES} entries"
            ),
            should_descend=should_descend,
        ):
            if not file_path.is_file():
                continue
            rel = _to_posix(str(file_path.relative_to(repo_root)))
            rel_parts = Path(rel).parts
            if any(_is_ignored(Path(part)) for part in rel_parts):
                continue
            if gitignore_patterns is not None:
                if _matches_gitignore(rel, gitignore_patterns):
                    continue
            elif _is_ignored(file_path):
                continue
            yield file_path

    # Bound the selected paths before sorting.  ``sorted(target.rglob(...))``
    # would allocate for every filesystem entry before the file-count
    # contract had a chance to run.
    files = _bounded_sorted_paths(
        eligible_files(),
        max_paths=MAX_FALLBACK_FILES,
        exceeded_message=(
            f"Hash guardrail exceeded: >{MAX_FALLBACK_FILES} files"
        ),
    )
    return [
        _to_posix(str(file_path.relative_to(repo_root)))
        for file_path in files
    ]


def git_latest_tag(
    repo_root: Path,
    prefix: str,
    ref: str = "HEAD",
) -> Optional[str]:
    """Find the latest tag reachable from one resolved ref.

    Callers performing a multi-component operation should pass the HEAD object
    ID captured at operation start rather than allowing each lookup to resolve
    a potentially different moving ``HEAD``.
    """
    prefix_error = git_tag_prefix_error(prefix)
    if prefix_error is not None:
        raise ValueError(f"Invalid literal Git tag prefix: {prefix_error}")
    try:
        # Prefer reachable tags from the selected commit to avoid unrelated
        # branch tags.
        result = _git_run(
            repo_root,
            ["describe", "--tags", "--match", f"{prefix}*", "--abbrev=0", ref],
        )
        tag = result.stdout.strip()
        ver = tag[len(prefix):] if tag.startswith(prefix) else None
        return ver or None  # Return None instead of empty string
    except subprocess.CalledProcessError:
        # There is no repository-wide fallback: an unreachable tag is not a
        # version of the captured source commit.
        return None


def _append_bounded_git_path(
    paths: List[str],
    raw_path: bytes,
    total_path_bytes: int,
) -> int:
    """Append one decoded Git path and return its bounded aggregate size."""
    if not raw_path:
        raise ValueError("Malformed empty path in Git output")
    if len(raw_path) > MAX_GIT_PATH_BYTES:
        raise GuardrailError(
            f"Git path exceeds the {MAX_GIT_PATH_BYTES}-byte limit"
        )
    if len(paths) >= MAX_GIT_STATUS_PATHS:
        raise GuardrailError(
            "Git status paths exceed the "
            f"{MAX_GIT_STATUS_PATHS}-path limit"
        )
    updated = total_path_bytes + len(raw_path)
    if updated > MAX_GIT_TOTAL_PATH_BYTES:
        raise GuardrailError(
            "Git paths exceed the "
            f"{MAX_GIT_TOTAL_PATH_BYTES}-byte aggregate limit"
        )
    paths.append(os.fsdecode(raw_path))
    return updated


def _parse_name_status_entries(
    fields: Iterable[bytes],
) -> List[Tuple[str, str]]:
    """Return bounded status/path pairs from streamed ``diff --name-status``.

    Renames contribute both the removed source and added destination.  Copies
    contribute only the destination because the source identity did not
    change.
    """
    fields_iter = iter(fields)
    paths: List[str] = []
    entries: List[Tuple[str, str]] = []
    total_path_bytes = 0

    def append(status: str, raw_path: bytes) -> None:
        nonlocal total_path_bytes
        total_path_bytes = _append_bounded_git_path(
            paths,
            raw_path,
            total_path_bytes,
        )
        entries.append((status, paths[-1]))

    while True:
        try:
            raw_status = next(fields_iter)
        except StopIteration:
            break
        try:
            status = raw_status.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("Malformed non-ASCII Git diff status") from exc
        if not status or status[0] not in "ACDMRTUXB":
            raise ValueError(f"Malformed Git diff status: {status!r}")
        try:
            source_path = next(fields_iter)
        except StopIteration as exc:
            raise ValueError("Truncated path in Git diff output") from exc
        if status.startswith(("R", "C")):
            try:
                destination_path = next(fields_iter)
            except StopIteration as exc:
                raise ValueError(
                    "Truncated rename/copy in Git diff output"
                ) from exc
            if status.startswith("R"):
                append(status, source_path)
            append(status, destination_path)
        else:
            append(status, source_path)
    return entries


def _parse_name_status_records(fields: Iterable[bytes]) -> List[str]:
    """Return only path identities from bounded name-status entries."""
    return [path for _status, path in _parse_name_status_entries(fields)]


def _git_name_status(
    repo_root: Path,
    args: List[str],
) -> List[Tuple[str, str]]:
    """Run one deterministic Git diff without helpers or quadratic rename work."""
    diff_index = next(
        (
            index
            for index, argument in enumerate(args)
            if argument in {"diff", "diff-tree"}
        ),
        None,
    )
    if diff_index is None:
        raise ValueError("Git name-status arguments must invoke diff or diff-tree")
    hardened_args = [
        *args[: diff_index + 1],
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        # Submodules are represented by their Gitlink object ID. Inspecting
        # their worktrees would both exceed that source model and let nested
        # repository filter commands execute during an otherwise data-only
        # operation. ``dirty`` still reports a changed checked-out Gitlink.
        "--ignore-submodules=dirty",
        *args[diff_index + 1 :],
    ]
    return _parse_name_status_entries(
        _iter_git_nul_records(
            repo_root,
            hardened_args,
            max_records=MAX_GIT_STATUS_FIELDS,
        )
    )


def _working_tree_name_status(
    repo_root: Path,
    base_ref: str,
    *,
    pathspec: Optional[str] = None,
    tracking_snapshot: Optional[GitSourceSnapshot] = None,
) -> List[Tuple[str, str]]:
    """Compare a commit with raw tracked worktree state without Git filters.

    Git porcelain worktree comparisons can execute repository-defined clean or
    process filters. Boundver instead reads tracked paths through its bounded
    source reader and compares them with immutable base-tree blobs. This uses
    the same CRLF normalization and mode rules as working-tree fingerprints.
    """
    if not base_ref or not base_ref.strip() or base_ref.lstrip().startswith("-"):
        raise ValueError("working-tree comparison requires a valid base ref")
    try:
        resolved_base = _git_run(
            repo_root,
            ["rev-parse", "--verify", f"{base_ref.strip()}^{{commit}}"],
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError) as exc:
        raise ValueError(
            f"Cannot resolve working-tree comparison base {base_ref!r}: "
            f"{_git_failure_detail(exc)}"
        ) from exc
    base_oid = _validated_git_object_id(
        resolved_base,
        "git working-tree comparison base lookup",
    )
    base_entries = _capture_tree_entries(repo_root, base_oid, "base")

    captured = tracking_snapshot or _capture_git_source_snapshot(
        repo_root, "index"
    )
    if captured.source != "index":
        raise ValueError(
            "working-tree comparison accepts only an index tracking snapshot"
        )

    normalized_pathspec = ""
    if pathspec is not None:
        normalized_pathspec = _to_posix(pathspec).strip("/")
        if normalized_pathspec == ".":
            normalized_pathspec = ""

    def selected(path: str) -> bool:
        return (
            not normalized_pathspec
            or path == normalized_pathspec
            or path.startswith(f"{normalized_pathspec}/")
        )

    paths = sorted(
        path
        for path in set(base_entries) | set(captured.tracked_paths)
        if selected(path)
    )
    if len(paths) > MAX_GIT_STATUS_PATHS:
        raise GuardrailError(
            "Working-tree comparison exceeds the "
            f"{MAX_GIT_STATUS_PATHS}-path limit"
        )

    # Local import avoids a module cycle: _hashing uses these Git primitives.
    from ._hashing import _normalize_hash_content, _read_path_content

    base_blob_oids = [
        base_entries[path].oid
        for path in paths
        if path in base_entries
        and base_entries[path].object_type == "blob"
    ]
    oid_counts = Counter(base_blob_oids)
    base_blobs = _iter_git_blobs(
        repo_root,
        base_blob_oids,
        # This is a repository change-reporting scan, not one component hash.
        # The path-count and per-blob ceilings remain in force. The separate
        # repository reporting budget avoids applying one component's 256 MiB
        # aggregate ceiling to a large multi-component repository.
        max_total_bytes=MAX_GIT_REPOSITORY_SCAN_BYTES,
    )
    sparse_blob_oids = []
    for path in paths:
        if path not in captured.skip_worktree_paths:
            continue
        try:
            (repo_root / path).lstat()
        except FileNotFoundError:
            current_entry = captured.entries.get(path)
            base_entry = base_entries.get(path)
            if (
                current_entry is not None
                and current_entry.object_type == "blob"
                and (
                    base_entry is None
                    or base_entry.object_type != "blob"
                    or base_entry.oid != current_entry.oid
                )
            ):
                sparse_blob_oids.append(current_entry.oid)
    sparse_blob_digests: Dict[str, bytes] = {}
    sparse_blob_total = 0
    sparse_blobs = _iter_git_blobs(
        repo_root,
        sparse_blob_oids,
        max_total_bytes=MAX_GIT_REPOSITORY_SCAN_BYTES,
    )
    try:
        for oid, raw_content in sparse_blobs:
            sparse_blob_total += len(raw_content)
            sparse_blob_digests[oid] = hashlib.sha256(
                _normalize_hash_content(raw_content)
            ).digest()
    finally:
        close = getattr(sparse_blobs, "close", None)
        if close is not None:
            close()
    seen_oids = set()
    duplicate_cache: Dict[str, bytes] = {}
    current_total = sparse_blob_total
    changes: List[Tuple[str, str]] = []
    deleted_by_identity: Dict[Tuple[str, str, bytes], List[str]] = {}
    added_by_identity: Dict[Tuple[str, str, bytes], List[str]] = {}

    def rename_key(identity: Tuple[str, str, bytes]) -> Tuple[str, str, bytes]:
        return (
            identity[0],
            identity[1],
            hashlib.sha256(identity[2]).digest(),
        )

    try:
        for path in paths:
            base_entry = base_entries.get(path)
            current_entry = captured.entries.get(path)
            current_is_tracked = path in captured.tracked_paths

            base_identity: Optional[Tuple[str, str, bytes]] = None
            if base_entry is not None:
                if base_entry.object_type == "blob":
                    if base_entry.oid in seen_oids:
                        base_digest = duplicate_cache[base_entry.oid]
                    else:
                        try:
                            streamed_oid, raw_content = next(base_blobs)
                        except StopIteration as exc:
                            raise ValueError(
                                f"Missing base blob while comparing {path}"
                            ) from exc
                        if streamed_oid != base_entry.oid:
                            raise ValueError(
                                "Base blob stream order disagreed with captured tree"
                            )
                        seen_oids.add(base_entry.oid)
                        base_digest = hashlib.sha256(
                            _normalize_hash_content(raw_content)
                        ).digest()
                        if oid_counts[base_entry.oid] > 1:
                            duplicate_cache[base_entry.oid] = base_digest
                    base_identity = (
                        base_entry.mode,
                        base_entry.object_type,
                        base_digest,
                    )
                else:
                    base_identity = (
                        base_entry.mode,
                        base_entry.object_type,
                        base_entry.oid.encode("ascii"),
                    )

            current_identity: Optional[Tuple[str, str, bytes]] = None
            if current_is_tracked:
                full_path = repo_root / path
                try:
                    full_path.lstat()
                except FileNotFoundError:
                    if path in captured.skip_worktree_paths:
                        if current_entry is None:
                            raise ValueError(
                                "Sparse index path has no captured tree entry: "
                                f"{path}"
                            )
                        if current_entry.object_type == "blob":
                            if (
                                base_entry is not None
                                and base_entry.object_type == "blob"
                                and base_entry.oid == current_entry.oid
                            ):
                                assert base_identity is not None
                                current_digest = base_identity[2]
                            else:
                                current_digest = sparse_blob_digests.get(
                                    current_entry.oid
                                )
                                if current_digest is None:
                                    # The path disappeared after the sparse
                                    # pre-scan. Preserve snapshot semantics with
                                    # one bounded fallback read for that race.
                                    raw_content = _git_cat_blob(
                                        repo_root,
                                        current_entry.oid,
                                        max_bytes=(
                                            MAX_GIT_REPOSITORY_SCAN_BYTES
                                            - current_total
                                        ),
                                    )
                                    current_total += len(raw_content)
                                    current_digest = hashlib.sha256(
                                        _normalize_hash_content(raw_content)
                                    ).digest()
                            current_identity = (
                                current_entry.mode,
                                current_entry.object_type,
                                current_digest,
                            )
                        else:
                            current_identity = (
                                current_entry.mode,
                                current_entry.object_type,
                                current_entry.oid.encode("ascii"),
                            )
                else:
                    if current_entry is None or current_entry.object_type == "blob":
                        content = _read_path_content(
                            repo_root,
                            full_path,
                            "working-tree",
                            max_bytes=(
                                MAX_GIT_REPOSITORY_SCAN_BYTES - current_total
                            ),
                            tracked_entry=current_entry,
                            core_filemode=captured.filemode,
                        )
                        current_total += content.source_size
                        current_identity = (
                            content.git_mode,
                            content.git_object_type,
                            hashlib.sha256(content).digest(),
                        )
                    else:
                        # Do not recurse into a submodule or invoke its Git.
                        current_identity = (
                            current_entry.mode,
                            current_entry.object_type,
                            current_entry.oid.encode("ascii"),
                        )

            if base_identity is None and current_identity is not None:
                changes.append(("A", path))
                added_by_identity.setdefault(rename_key(current_identity), []).append(
                    path
                )
            elif base_identity is not None and current_identity is None:
                changes.append(("D", path))
                deleted_by_identity.setdefault(rename_key(base_identity), []).append(
                    path
                )
            elif base_identity != current_identity:
                assert base_identity is not None
                assert current_identity is not None
                status = (
                    "T"
                    if base_identity[:2] != current_identity[:2]
                    else "M"
                )
                changes.append((status, path))
        try:
            next(base_blobs)
        except StopIteration:
            pass
        else:
            raise ValueError("Unexpected extra base blob in comparison stream")
    finally:
        close = getattr(base_blobs, "close", None)
        if close is not None:
            close()

    renamed_paths: Dict[str, str] = {}
    for identity in sorted(set(deleted_by_identity) & set(added_by_identity)):
        removed = deleted_by_identity[identity]
        added = added_by_identity[identity]
        for source_path, destination_path in zip(removed, added):
            renamed_paths[source_path] = "R100"
            renamed_paths[destination_path] = "R100"
    return [
        (renamed_paths.get(path, status), path)
        for status, path in changes
    ]


def changed_paths_since_ref(
    repo_root: Path,
    base_ref: str,
    source: str = "working-tree",
    snapshot: Optional[GitSourceSnapshot] = None,
) -> List[str]:
    """Return tracked path identities changed from *base_ref* to *source*."""
    if source not in SOURCE_MODE_SET:
        raise ValueError(f"Unknown source mode: {source!r}")
    resolved_base = _resolve_git_commit(
        repo_root,
        base_ref,
        label="changed-from",
    )
    try:
        if source in {"head", "index"}:
            captured = snapshot or _capture_git_source_snapshot(repo_root, source)
            if captured.source != source:
                raise ValueError(
                    f"Captured {captured.source!r} snapshot cannot serve "
                    f"{source!r} changed-path selection"
                )
            target = (
                captured.head_oid or captured.tree_oid
                if source == "head"
                else captured.tree_oid
            )
            changed_paths = _parse_name_status_records(
                _iter_git_nul_records(
                    repo_root,
                    [
                        "diff",
                        "--name-status",
                        "-z",
                        "--find-renames",
                        resolved_base,
                        target,
                        "--",
                    ],
                    max_records=MAX_GIT_STATUS_FIELDS,
                )
            )
        else:
            if snapshot is not None and snapshot.source != "index":
                raise ValueError(
                    "working-tree changed-path selection accepts only an "
                    "index tracking snapshot"
                )
            changed_paths = [
                path
                for _status, path in _working_tree_name_status(
                    repo_root,
                    resolved_base,
                    tracking_snapshot=snapshot,
                )
            ]
    except subprocess.CalledProcessError as exc:
        raise ValueError(
            "Unable to diff from Git ref "
            f"{_bounded_diagnostic_repr(base_ref, max_chars=256)}"
        ) from exc
    return sorted(set(changed_paths))


def changed_components_since_ref(
    config: dict,
    repo_root: Path,
    base_ref: str,
    source: str = "working-tree",
    snapshot: Optional[GitSourceSnapshot] = None,
) -> List[str]:
    """Return component names changed from *base_ref* to *source*."""
    changed_files = changed_paths_since_ref(
        repo_root, base_ref, source, snapshot
    )
    changed: List[str] = []
    config_names = {
        "boundary.config.json", "boundary.config.yaml", "boundary.config.yml",
        "boundary.config.toml",
    }
    if any(Path(f).name in config_names for f in changed_files):
        return sorted(config.get("components", {}))
    tag_versioned = {
        name
        for name, component in config.get("components", {}).items()
        if isinstance(component, dict)
        and isinstance(component.get("version_source"), dict)
        and "git_tag_prefix" in component["version_source"]
    }
    components = config.get("components", {})
    matched_files: set = set()
    for cname, comp in components.items():
        raw_path = str(comp.get("path", "")).strip()
        cpath = _to_posix(os.path.normpath(raw_path)) if raw_path else ""
        if cpath in {"", "."}:
            if changed_files:
                changed.append(cname)
                matched_files.update(changed_files)
            continue
        prefix = f"{cpath}/"
        component_matches = {
            f for f in changed_files if f == cpath or f.startswith(prefix)
        }
        if component_matches:
            changed.append(cname)
            matched_files.update(component_matches)

    # A changed path outside every currently configured component may be a
    # deleted/moved component or another config input.  Selecting everything is
    # conservative, but avoids a false-clean result when the old path is no
    # longer available to map precisely.
    if changed_files and matched_files != set(changed_files):
        return sorted(components)
    return sorted(set(changed) | tag_versioned)


def dirty_component_paths(repo_root: Path, component_paths: List[str]) -> List[str]:
    """Return component paths that have uncommitted changes (staged or unstaged).

    Used to warn users when --source head is used but boundary files have been
    modified locally.
    """
    try:
        captured = _capture_git_source_snapshot(repo_root, "index")
        if captured.head_oid is None:
            return sorted(component_paths)
        staged = _git_name_status(
            repo_root,
            [
                "diff",
                "--name-status",
                "-z",
                captured.head_oid,
                captured.tree_oid,
                "--",
            ],
        )
        unstaged = _working_tree_name_status(
            repo_root,
            captured.head_oid,
            tracking_snapshot=captured,
        )
        untracked = list(
            _iter_bounded_git_paths(
                repo_root,
                [
                    "--literal-pathspecs",
                    "ls-files",
                    "--others",
                    "--exclude-standard",
                    "-z",
                    "--",
                ],
            )
        )
    except subprocess.CalledProcessError:
        return []
    dirty_files = sorted(
        {path for _status, path in staged}
        | {path for _status, path in unstaged}
        | set(untracked)
    )
    dirty: List[str] = []
    for cpath in component_paths:
        cpath_norm = cpath.rstrip("/")
        if cpath_norm in {"", "."}:
            if dirty_files:
                dirty.append(cpath)
            continue
        prefix = f"{cpath_norm}/"
        if any(f == cpath_norm or f.startswith(prefix) for f in dirty_files):
            dirty.append(cpath)
    return sorted(dirty)
