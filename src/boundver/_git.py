"""Git helper primitives for boundver."""

import os
import stat
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import (
    BinaryIO,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Tuple,
)

from ._config_contract import git_tag_prefix_error
from ._utils import (
    GuardrailError,
    SOURCE_MODE_SET,
    _bounded_diagnostic_repr,
    _bounded_sorted_paths,
    _iter_bounded_filesystem_paths,
    _match_path_glob,
    _match_text_glob,
    _read_bounded_path_bytes,
    _validate_glob_pattern_complexity,
)


MAX_GIT_BLOB_BYTES = 50 * 1024 * 1024
MAX_GIT_BATCH_BYTES = 256 * 1024 * 1024
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
_GIT_STREAM_CHUNK_BYTES = 64 * 1024
_GIT_DIAGNOSTIC_TRUNCATION = b"\n...[Git diagnostic truncated by boundver]"
MAX_FALLBACK_FILES = 50_000
MAX_FALLBACK_TRAVERSAL_ENTRIES = 200_000
MAX_GITIGNORE_BYTES = 1024 * 1024
MAX_GITIGNORE_PATTERN_BYTES = MAX_GIT_PATH_BYTES
MAX_GITIGNORE_RULES = 10_000
MAX_GITIGNORE_MATCH_STEPS = 10_000_000


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


def _read_bounded_git_diagnostic(stderr_file: BinaryIO) -> bytes:
    """Return capped subprocess diagnostics with an explicit truncation mark."""
    stderr_file.seek(0)
    diagnostic = stderr_file.read(MAX_GIT_DIAGNOSTIC_BYTES + 1)
    if len(diagnostic) <= MAX_GIT_DIAGNOSTIC_BYTES:
        return diagnostic
    return diagnostic[:MAX_GIT_DIAGNOSTIC_BYTES] + _GIT_DIAGNOSTIC_TRUNCATION


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
    tree written from the index once.  Reading blobs by object ID prevents a
    concurrent ref/index update from producing a hybrid lockfile.
    """

    source: str
    tree_oid: str
    entries: Dict[str, GitTreeEntry]
    head_oid: Optional[str] = None
    filemode: bool = True


def git_root() -> Path:
    """Find the repository root through the bounded, lossless text transport."""
    result = _git_run(Path.cwd(), ["rev-parse", "--show-toplevel"])
    return Path(result.stdout.strip())


def _git_run(repo_root: Path, args: List[str]) -> subprocess.CompletedProcess:
    """Run git against a specific repository root regardless of process CWD."""
    command = ["git", "-C", str(repo_root), *args]
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None

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
    command = ["git", "-C", str(repo_root), *args]
    record_limit = MAX_GIT_TREE_ENTRIES if max_records is None else max_records
    if record_limit < 0:
        raise ValueError("Git listing record limit must be non-negative")
    with tempfile.TemporaryFile() as stderr_file:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
        )
        assert proc.stdout is not None
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
            if returncode != 0:
                raise subprocess.CalledProcessError(
                    returncode,
                    command,
                    stderr=_read_bounded_git_diagnostic(stderr_file),
                )
            if pending:
                raise ValueError("Truncated NUL-delimited Git listing output")
        finally:
            proc.stdout.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait()


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


def _capture_git_source_snapshot(repo_root: Path, source: str) -> GitSourceSnapshot:
    """Capture one immutable tree for a ``head`` or ``index`` operation."""
    if source not in {"head", "index"}:
        raise ValueError(f"Cannot capture a Git snapshot for source {source!r}")

    head_oid = _resolve_head_oid(repo_root)
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

    try:
        entries = _collect_ls_tree_entries(
            _iter_git_nul_records(
                repo_root,
                ["ls-tree", "-r", "-z", "--full-tree", treeish],
            )
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise ValueError(
            f"Cannot enumerate captured {source} tree {treeish}: "
            f"git ls-tree failed ({_git_failure_detail(exc)})"
        ) from exc
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
    )


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
    command = ["git", "-C", str(repo_root), "show", ref]
    # ``capture_output`` would buffer the entire object before the size check.
    # Bound the read itself and terminate Git as soon as the limit is crossed.
    with tempfile.TemporaryFile() as stderr_file:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
        )
        assert proc.stdout is not None
        try:
            content = proc.stdout.read(effective_limit + 1)
            if len(content) > effective_limit:
                proc.kill()
                proc.wait()
                raise GuardrailError(
                    f"Hash guardrail exceeded: Git blob too large "
                    f"(>{effective_limit} bytes) for ref {ref!r}"
                )
            returncode = proc.wait()
            if returncode != 0:
                raise subprocess.CalledProcessError(
                    returncode,
                    command,
                    output=content,
                    stderr=_read_bounded_git_diagnostic(stderr_file),
                )
            return content
        finally:
            proc.stdout.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait()


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


def _iter_git_blobs(
    repo_root: Path,
    refs: List[str],
    *,
    max_total_bytes: int = MAX_GIT_BATCH_BYTES,
    remaining_bytes: Optional[Callable[[], int]] = None,
) -> Iterator[Tuple[str, bytes]]:
    """Yield unique Git blobs under a pre-read aggregate byte ceiling."""
    if max_total_bytes < 0:
        raise ValueError("Git blob aggregate byte limit must be non-negative")
    effective_total_limit = min(max_total_bytes, MAX_GIT_BATCH_BYTES)
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

    command = ["git", "-C", str(repo_root), "cat-file", "--batch"]
    with tempfile.TemporaryFile() as stderr_file:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
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
                        stderr=_read_bounded_git_diagnostic(stderr_file),
                    ) from exc
                header = proc.stdout.readline(MAX_GIT_BATCH_HEADER_BYTES + 1)
                if not header.endswith(b"\n"):
                    if not header:
                        returncode = proc.wait()
                        if returncode != 0:
                            raise subprocess.CalledProcessError(
                                returncode,
                                command,
                                stderr=_read_bounded_git_diagnostic(stderr_file),
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
                total_bytes += len(content)
                yield ref, content

            proc.stdin.close()
            returncode = proc.wait()
            if returncode != 0:
                raise subprocess.CalledProcessError(
                    returncode,
                    command,
                    stderr=_read_bounded_git_diagnostic(stderr_file),
                )
        finally:
            if not proc.stdin.closed:
                try:
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            proc.stdout.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait()


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
    """Run a Git name-status command without materializing unbounded stdout."""
    return _parse_name_status_entries(
        _iter_git_nul_records(
            repo_root,
            args,
            max_records=MAX_GIT_STATUS_FIELDS,
        )
    )


def changed_paths_since_ref(
    repo_root: Path,
    base_ref: str,
    source: str = "working-tree",
    snapshot: Optional[GitSourceSnapshot] = None,
) -> List[str]:
    """Return tracked path identities changed from *base_ref* to *source*."""
    if not base_ref or not base_ref.strip():
        raise ValueError("--changed-from requires a non-empty Git ref")
    ref = base_ref.strip()
    if ref.startswith("-"):
        raise ValueError(f"Invalid Git ref: {ref!r}")
    if source not in SOURCE_MODE_SET:
        raise ValueError(f"Unknown source mode: {source!r}")
    try:
        resolved_base = _git_run(
            repo_root, ["rev-parse", "--verify", f"{ref}^{{commit}}"]
        ).stdout.strip()
        if not resolved_base:
            raise subprocess.CalledProcessError(1, ["git", "rev-parse", ref])
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
            diff_args = [
                "diff",
                "--name-status",
                "-z",
                "--find-renames",
                resolved_base,
                target,
                "--",
            ]
        else:
            if snapshot is not None and snapshot.source != "index":
                raise ValueError(
                    "working-tree changed-path selection accepts only an "
                    "index tracking snapshot"
                )
            diff_args = [
                "diff",
                "--name-status",
                "-z",
                "--find-renames",
                resolved_base,
                "--",
            ]
        changed_paths = _parse_name_status_records(
            _iter_git_nul_records(
                repo_root,
                diff_args,
                max_records=MAX_GIT_STATUS_FIELDS,
            )
        )
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"Unable to diff from Git ref {ref!r}") from exc
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
    dirty_files: List[str] = []
    total_path_bytes = 0
    try:
        records = iter(
            _iter_git_nul_records(
                repo_root,
                [
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=all",
                    "--",
                ],
                max_records=MAX_GIT_STATUS_FIELDS,
            )
        )
        for record in records:
            if len(record) < 4 or record[2:3] != b" ":
                raise ValueError("Malformed NUL-delimited git status output")
            status = record[:2]
            total_path_bytes = _append_bounded_git_path(
                dirty_files, record[3:], total_path_bytes
            )
            if b"R" in status or b"C" in status:
                try:
                    source_path = next(records)
                except StopIteration as exc:
                    raise ValueError(
                        "Truncated rename in git status output"
                    ) from exc
                total_path_bytes = _append_bounded_git_path(
                    dirty_files, source_path, total_path_bytes
                )
    except subprocess.CalledProcessError:
        return []
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
