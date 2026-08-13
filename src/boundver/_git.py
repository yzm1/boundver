"""Git helper primitives for boundver."""

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from ._utils import GuardrailError


MAX_GIT_BLOB_BYTES = 50 * 1024 * 1024
MAX_FALLBACK_FILES = 50_000


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
    """Find the repository root."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    )
    return Path(result.stdout.strip())


def _git_run(repo_root: Path, args: List[str]) -> subprocess.CompletedProcess:
    """Run git against a specific repository root regardless of process CWD."""
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True, text=True, check=True,
    )


def _git_run_bytes(repo_root: Path, args: List[str]) -> subprocess.CompletedProcess:
    """Run Git with byte output for NUL-delimited, filename-safe commands."""
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True, text=False, check=True,
    )


def _decode_nul_paths(data: bytes) -> List[str]:
    """Decode Git's NUL-delimited path output without C-quote mangling."""
    return [os.fsdecode(raw) for raw in data.split(b"\0") if raw]


def _resolve_head_oid(repo_root: Path) -> Optional[str]:
    """Resolve HEAD once, returning ``None`` for an unborn repository."""
    try:
        result = _git_run(repo_root, ["rev-parse", "--verify", "HEAD^{commit}"])
    except subprocess.CalledProcessError:
        return None
    oid = result.stdout.strip()
    return oid or None


def _is_git_repository(repo_root: Path) -> bool:
    """Return whether *repo_root* is inside a readable Git work tree.

    This deliberately distinguishes a directory with no repository (where the
    documented working-tree filesystem fallback is useful) from a real
    repository whose index cannot be snapshotted.  The latter includes
    unresolved merge stages and must fail closed.
    """
    try:
        result = _git_run(repo_root, ["rev-parse", "--is-inside-work-tree"])
    except subprocess.CalledProcessError:
        return False
    return result.stdout.strip().lower() == "true"


def _parse_ls_tree_entries(data: bytes) -> Dict[str, GitTreeEntry]:
    """Parse ``git ls-tree -r -z --full-tree`` output without path quoting."""
    entries: Dict[str, GitTreeEntry] = {}
    for record in data.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            raw_mode, raw_type, raw_oid = header.split(b" ", 2)
            mode = raw_mode.decode("ascii")
            object_type = raw_type.decode("ascii")
            oid = raw_oid.decode("ascii")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"Malformed git ls-tree record: {record!r}") from exc
        if len(mode) != 6 or not mode.isdigit():
            raise ValueError(f"Malformed Git mode {mode!r} for {os.fsdecode(raw_path)!r}")
        if object_type not in {"blob", "commit"}:
            raise ValueError(
                f"Unsupported Git object type {object_type!r} for "
                f"{os.fsdecode(raw_path)!r}"
            )
        path = os.fsdecode(raw_path)
        if path in entries:
            raise ValueError(f"Duplicate path in captured Git tree: {path!r}")
        entries[path] = GitTreeEntry(path, mode, object_type, oid)
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
        except subprocess.CalledProcessError as exc:
            raise ValueError("Cannot capture index as a complete Git tree") from exc
        treeish = result.stdout.strip()
        if not treeish:
            raise ValueError("git write-tree returned an empty object ID")

    try:
        listed = _git_run_bytes(
            repo_root,
            ["ls-tree", "-r", "-z", "--full-tree", treeish],
        )
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"Cannot enumerate captured {source} tree {treeish}") from exc
    try:
        filemode_result = _git_run(repo_root, ["config", "--bool", "core.filemode"])
    except subprocess.CalledProcessError:
        core_filemode = True
    else:
        core_filemode = filemode_result.stdout.strip().lower() != "false"
    return GitSourceSnapshot(
        source=source,
        tree_oid=treeish,
        entries=_parse_ls_tree_entries(listed.stdout),
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
) -> tuple:
    """Return canonical Git mode/type for a tracked working-tree path."""
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


def _git_cat_blob(repo_root: Path, ref: str) -> bytes:
    """Read a single git blob at the given ref as raw bytes (no text-mode CRLF conversion)."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", ref],
        capture_output=True, text=False, check=True,
    )
    if len(result.stdout) > MAX_GIT_BLOB_BYTES:
        raise GuardrailError(
            f"Hash guardrail exceeded: Git blob too large "
            f"({len(result.stdout)} bytes) for ref {ref!r}"
        )
    return result.stdout


def _git_batch_cat(repo_root: Path, refs: List[str]) -> Dict[str, bytes]:
    """Batch-read multiple git objects via ``git cat-file --batch``.

    All objects are fetched in a single subprocess, replacing O(N) ``git show``
    calls with O(1).  Returns ``{ref: raw_bytes}``.  Missing, malformed,
    truncated, non-blob, and oversized responses raise instead of being
    confused with a valid empty blob.
    """
    if not refs:
        return {}
    # The widely supported batch protocol uses newline-delimited requests.
    # Read the rare refs containing CR/LF individually so valid Git filenames
    # never desynchronize the protocol.
    batch_refs: List[str] = []
    blobs: Dict[str, bytes] = {}
    for r in refs:
        if "\n" in r or "\r" in r:
            try:
                blobs[r] = _git_cat_blob(repo_root, r)
            except subprocess.CalledProcessError as exc:
                raise ValueError(
                    f"Git blob not found for ref containing a newline: {r!r}"
                ) from exc
        else:
            batch_refs.append(r)
    if not batch_refs:
        return blobs
    inp = b"\n".join(os.fsencode(ref) for ref in batch_refs) + b"\n"
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "--batch"],
        input=inp,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, ["git", "cat-file", "--batch"], proc.stderr
        )
    data = proc.stdout
    pos = 0
    for ref in batch_refs:
        try:
            nl = data.index(b"\n", pos)
        except ValueError as exc:
            raise ValueError(
                f"Truncated git cat-file response before header for {ref!r}"
            ) from exc
        header = data[pos:nl].decode("ascii", errors="replace")
        pos = nl + 1
        # Header format: "<ref> SP <type> SP <size>" or "<ref> SP missing".
        # Refs may contain spaces (e.g. paths with spaces), so split from the
        # right where we know the fixed-format suffix lives.
        if header.endswith(" missing"):
            raise ValueError(f"Git blob not found for ref {ref!r}")
        # Expect "<ref> <type> <size>" — size is always the last token.
        last_space = header.rfind(" ")
        if last_space < 0:
            raise ValueError(
                f"Malformed git cat-file header for {ref!r}: {header!r}"
            )
        type_space = header.rfind(" ", 0, last_space)
        if type_space < 0:
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
        end = pos + size
        if end >= len(data):
            raise ValueError(
                f"Truncated git cat-file content for {ref!r}: "
                f"expected {size} bytes"
            )
        blobs[ref] = data[pos:end]
        if data[end:end + 1] != b"\n":
            raise ValueError(
                f"Malformed git cat-file terminator for {ref!r}"
            )
        pos = end + 1
    if pos != len(data):
        raise ValueError("Unexpected trailing data from git cat-file --batch")
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
    rules = _GitignoreRules()
    for line in gi.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rules.add(line)
    return rules


class _GitignoreRules:
    """Structured gitignore rules supporting negation (!) and ** globs."""

    def __init__(self) -> None:
        self._rules: List[tuple] = []  # List[tuple[bool, str]]  (negate, pattern)

    def add(self, raw_line: str) -> None:
        negate = raw_line.startswith("!")
        pattern = raw_line[1:] if negate else raw_line
        pattern = pattern.rstrip("/")
        if pattern:
            self._rules.append((negate, pattern))

    def is_ignored(self, rel_path: str) -> bool:
        """Return True if rel_path should be excluded per gitignore rules."""
        parts = rel_path.replace("\\", "/").split("/")
        ignored = False
        for negate, pattern in self._rules:
            if self._matches(rel_path, parts, pattern):
                ignored = not negate
        return ignored

    @staticmethod
    def _matches(rel_path: str, parts: List[str], pattern: str) -> bool:
        import fnmatch
        # Handle ** recursive wildcard by converting to regex-friendly form
        if "**" in pattern:
            # "**/foo" matches foo at any depth
            # "foo/**/bar" matches foo/bar, foo/x/bar, foo/x/y/bar etc.
            regex = _gitignore_pattern_to_regex(pattern)
            import re as _re
            return _re.match(regex, rel_path) is not None
        # Pattern without / matches any path component
        if "/" not in pattern:
            return any(fnmatch.fnmatch(part, pattern) for part in parts)
        # Pattern with / — match as prefix (directory) or full glob from root
        if rel_path.startswith(pattern + "/") or rel_path == pattern:
            return True
        return fnmatch.fnmatch(rel_path, pattern)


def _gitignore_pattern_to_regex(pattern: str) -> str:
    """Convert a gitignore pattern with ** into a regex string."""
    import re as _re
    # Split on ** to get segments between recursive wildcards.
    # "foo/**/bar" → ["foo/", "/bar"]
    # "**/test" → ["", "/test"]
    # "src/**" → ["src/", ""]
    # "**" → ["", ""]
    segments = pattern.split("**")
    num_segments = len(segments)
    regex_parts = []
    for i, seg in enumerate(segments):
        # Strip slashes that separate from the ** wildcard
        if i > 0:
            seg = seg.lstrip("/")
        if i < num_segments - 1:
            seg = seg.rstrip("/")
        # Convert the segment's glob characters to regex
        part = ""
        for ch in seg:
            if ch == "*":
                part += "[^/]*"
            elif ch == "?":
                part += "[^/]"
            elif ch == "/":
                part += "/"
            else:
                part += _re.escape(ch)
        regex_parts.append(part)

    # Join with ** separators that depend on position:
    result = ""
    for i, part in enumerate(regex_parts):
        if i > 0:
            # Insert the ** separator between this part and the previous
            prev_empty = (regex_parts[i - 1] == "")
            curr_empty = (part == "")
            if curr_empty and i == num_segments - 1:
                # Trailing ** — match anything that follows the previous segment
                # Require a slash separator if preceding segment is non-empty
                if not prev_empty:
                    result += "/.*"
                else:
                    result += ".*"
            elif prev_empty and i == 1:
                # Leading ** (already handled as prefix)
                result += "(?:.*/)?" + part
            else:
                # Middle ** — optional path segments with required slash
                result += "(?:/.*/|/)" + part
        else:
            if part == "" and num_segments > 1:
                # Leading ** — will be handled by next iteration
                pass
            else:
                result += part
    return result + "$"


def _matches_gitignore(rel_path: str, patterns: "_GitignoreRules") -> bool:
    """Check if a repo-relative path matches gitignore rules."""
    return patterns.is_ignored(rel_path)


def list_head_files(repo_root: Path, path: str) -> List[str]:
    """List files at a repo-relative path as represented in HEAD."""
    try:
        result = _git_run_bytes(
            repo_root,
            ["--literal-pathspecs", "ls-tree", "-r", "-z", "--name-only", "HEAD", "--", path],
        )
    except subprocess.CalledProcessError:
        return []

    files = _decode_nul_paths(result.stdout)
    if files:
        return files

    try:
        result = _git_run(repo_root, ["cat-file", "-t", f"HEAD:{path}"])
    except subprocess.CalledProcessError:
        return []
    return [_to_posix(path)] if result.stdout.strip() == "blob" else []


def _list_files_for_source(repo_root: Path, repo_rel_path: str, source: str) -> List[str]:
    if source == "head":
        return list_head_files(repo_root, repo_rel_path)
    # Index and working-tree use the same tracked file set.  The selected
    # source only controls whether content comes from the index or from disk.
    args = ["--literal-pathspecs", "ls-files", "--cached", "-z"]
    args.extend(["--", repo_rel_path])
    try:
        result = _git_run_bytes(repo_root, args)
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
        result_files = _decode_nul_paths(result.stdout)

    # For working-tree source, exclude files that are tracked but deleted on disk.
    if result_files and source == "working-tree":
        result_files = [
            f
            for f in result_files
            if (repo_root / f).exists() or (repo_root / f).is_symlink()
        ]

    # An empty successful result is authoritative (for example, a legitimate
    # empty index). The one usability exception is an unborn repository in
    # working-tree mode: before the first commit there is no tracked-file view,
    # so use the bounded filesystem fallback to support initial setup.
    if not result_files and source == "working-tree":
        try:
            _git_run(repo_root, ["rev-parse", "--verify", "HEAD"])
        except subprocess.CalledProcessError:
            git_failed = True
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
    results: List[str] = []
    for f in sorted(target.rglob("*")):
        if not f.is_file():
            continue
        rel = _to_posix(str(f.relative_to(repo_root)))
        rel_parts = Path(rel).parts
        if any(_is_ignored(Path(part)) for part in rel_parts):
            continue
        if gitignore_patterns is not None:
            if _matches_gitignore(rel, gitignore_patterns):
                continue
        elif _is_ignored(f):
            continue
        results.append(rel)
        # Enumerate one sentinel beyond the contract limit. Returning a
        # truncated list would silently exclude files from every fallback
        # fingerprint instead of enforcing the advertised guardrail.
        if len(results) > MAX_FALLBACK_FILES:
            raise GuardrailError(
                f"Hash guardrail exceeded: >{MAX_FALLBACK_FILES} files"
            )
    return results


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


def changed_components_since_ref(config: dict, repo_root: Path, base_ref: str) -> List[str]:
    """Return component names with tracked changes since `base_ref`."""
    if not base_ref or not base_ref.strip():
        raise ValueError("--changed-from requires a non-empty Git ref")
    ref = base_ref.strip()
    if ref.startswith("-"):
        raise ValueError(f"Invalid Git ref: {ref!r}")
    try:
        _git_run(repo_root, ["rev-parse", "--verify", f"{ref}^{{commit}}"])
        result = _git_run_bytes(repo_root, ["diff", "--name-only", "-z", ref, "--"])
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"Unable to diff from Git ref {ref!r}") from exc
    changed_files = _decode_nul_paths(result.stdout)
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
        result = _git_run_bytes(
            repo_root,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--"],
        )
    except subprocess.CalledProcessError:
        return []
    records = [raw for raw in result.stdout.split(b"\0") if raw]
    dirty_files: List[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        if len(record) < 3:
            raise ValueError("Malformed NUL-delimited git status output")
        status = record[:2]
        if record[2:3] != b" ":
            raise ValueError("Malformed NUL-delimited git status output")
        dirty_files.append(os.fsdecode(record[3:]))
        if b"R" in status or b"C" in status:
            index += 1
            if index >= len(records):
                raise ValueError("Truncated rename in git status output")
            dirty_files.append(os.fsdecode(records[index]))
        index += 1
    dirty: List[str] = []
    for cpath in component_paths:
        cpath_norm = cpath.rstrip("/")
        prefix = f"{cpath_norm}/"
        if any(f == cpath_norm or f.startswith(prefix) for f in dirty_files):
            dirty.append(cpath)
    return sorted(dirty)
