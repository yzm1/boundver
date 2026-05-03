"""Git helper primitives for boundver."""

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional


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


def _git_cat_blob(repo_root: Path, ref: str) -> bytes:
    """Read a single git blob at the given ref as raw bytes (no text-mode CRLF conversion)."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", ref],
        capture_output=True, text=False, check=True,
    )
    # Guardrail: reject blobs larger than the per-file limit (50 MiB).
    _MAX_BLOB = 50 * 1024 * 1024
    if len(result.stdout) > _MAX_BLOB:
        raise ValueError(
            f"Git blob too large ({len(result.stdout)} bytes) for ref {ref!r}"
        )
    return result.stdout


def _git_batch_cat(repo_root: Path, refs: List[str]) -> Dict[str, bytes]:
    """Batch-read multiple git objects via ``git cat-file --batch``.

    All objects are fetched in a single subprocess, replacing O(N) ``git show``
    calls with O(1).  Returns ``{ref: raw_bytes}``.  Missing objects map to
    ``b""``.  Raises ``subprocess.CalledProcessError`` on git failure.
    """
    if not refs:
        return {}
    # Reject refs containing newlines — they would desynchronize the batch
    # protocol (git uses newline as the request delimiter).
    sanitized = []
    for r in refs:
        if "\n" in r or "\r" in r:
            raise ValueError(
                f"git cat-file ref contains newline (possible filename with embedded newline): {r!r}"
            )
        sanitized.append(r)
    inp = "\n".join(sanitized) + "\n"
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "--batch"],
        input=inp.encode("utf-8"),
        capture_output=True,
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, ["git", "cat-file", "--batch"], proc.stderr
        )
    blobs: Dict[str, bytes] = {}
    data = proc.stdout
    pos = 0
    for i, ref in enumerate(refs):
        try:
            nl = data.index(b"\n", pos)
        except ValueError:
            # Truncated output — remaining refs get empty bytes.
            for remaining in refs[i:]:
                blobs.setdefault(remaining, b"")
            break
        header = data[pos:nl].decode("ascii", errors="replace")
        pos = nl + 1
        # Header format: "<ref> SP <type> SP <size>" or "<ref> SP missing".
        # Refs may contain spaces (e.g. paths with spaces), so split from the
        # right where we know the fixed-format suffix lives.
        if header.endswith(" missing"):
            blobs[ref] = b""
            continue
        # Expect "<ref> <type> <size>" — size is always the last token.
        last_space = header.rfind(" ")
        if last_space < 0:
            blobs[ref] = b""
            continue
        try:
            size = int(header[last_space + 1:])
        except ValueError:
            # Cannot determine content size; stream is now desynchronized.
            for remaining in refs[i:]:
                blobs.setdefault(remaining, b"")
            break
        # Guardrail: skip objects exceeding per-file limit.
        _MAX_BLOB = 50 * 1024 * 1024
        if size > _MAX_BLOB:
            blobs[ref] = b""
            pos += size + 1
            continue
        blobs[ref] = data[pos : pos + size]
        pos += size + 1  # skip trailing LF after content
    return blobs


def _to_posix(rel_path: str) -> str:
    return rel_path.replace("\\", "/")


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
        import fnmatch
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
        result = _git_run(repo_root, ["ls-tree", "-r", "--name-only", "HEAD", path])
    except subprocess.CalledProcessError:
        return []

    files = [_to_posix(line.strip()) for line in result.stdout.splitlines() if line.strip()]
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
    args = ["ls-files"]
    if source == "index":
        args.append("--cached")
    else:
        # working-tree: include tracked AND untracked (but not ignored) files,
        # but exclude deleted tracked files (--deleted lists them for removal).
        args.extend(["--cached", "--others", "--exclude-standard"])
    args.extend(["--", repo_rel_path])
    try:
        result = _git_run(repo_root, args)
    except subprocess.CalledProcessError:
        result_files: List[str] = []
    else:
        result_files = [_to_posix(line.strip()) for line in result.stdout.splitlines() if line.strip()]

    # For working-tree source, exclude files that are tracked but deleted on disk.
    if result_files and source == "working-tree":
        result_files = [f for f in result_files if (repo_root / f).exists()]

    if result_files:
        return result_files

    # Fallback for non-git test/runtime environments: local filesystem enumeration.
    import sys as _sys
    print(
        "WARNING: git file listing returned no results; falling back to filesystem enumeration. "
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
    _MAX_FALLBACK_FILES = 50000
    for f in sorted(target.rglob("*")):
        if not f.is_file():
            continue
        rel = _to_posix(str(f.relative_to(repo_root)))
        if gitignore_patterns is not None:
            if _matches_gitignore(rel, gitignore_patterns):
                continue
        elif _is_ignored(f):
            continue
        results.append(rel)
        if len(results) >= _MAX_FALLBACK_FILES:
            break
    return results


def git_latest_tag(repo_root: Path, prefix: str) -> Optional[str]:
    """Find the latest reachable git tag matching a prefix, extract version part."""
    try:
        # Prefer reachable tags from current HEAD to avoid unrelated branch tags.
        result = _git_run(repo_root, ["describe", "--tags", "--match", f"{prefix}*", "--abbrev=0"])
        tag = result.stdout.strip()
        ver = tag[len(prefix):] if tag.startswith(prefix) else None
        return ver or None  # Return None instead of empty string
    except subprocess.CalledProcessError:
        try:
            # Fallback for repos where describe cannot resolve (e.g. shallow/no reachable matches).
            result = _git_run(repo_root, ["tag", "--list", f"{prefix}*", "--sort=-v:refname"])
            tags = [t for t in result.stdout.strip().split("\n") if t]
            if tags:
                ver = tags[0][len(prefix):]
                return ver or None  # Return None instead of empty string
            return None
        except subprocess.CalledProcessError:
            return None


def changed_components_since_ref(config: dict, repo_root: Path, base_ref: str) -> List[str]:
    """Return component names with tracked changes since `base_ref`."""
    if not base_ref or not base_ref.strip():
        return []
    ref = base_ref.strip()
    if ref.startswith("-"):
        return []
    try:
        result = _git_run(repo_root, ["diff", "--name-only", ref, "--"])
    except subprocess.CalledProcessError:
        import sys as _sys
        print(
            f"WARNING: git diff failed for ref {ref!r}; assuming no changes.",
            file=_sys.stderr,
        )
        return []
    changed_files = [_to_posix(line.strip()) for line in result.stdout.splitlines() if line.strip()]
    changed: List[str] = []
    for cname, comp in config.get("components", {}).items():
        cpath = _to_posix(str(comp.get("path", "")).rstrip("/"))
        if not cpath:
            continue
        prefix = f"{cpath}/"
        if any(f == cpath or f.startswith(prefix) for f in changed_files):
            changed.append(cname)
    return sorted(changed)


def dirty_component_paths(repo_root: Path, component_paths: List[str]) -> List[str]:
    """Return component paths that have uncommitted changes (staged or unstaged).

    Used to warn users when --source head is used but boundary files have been
    modified locally.
    """
    try:
        result = _git_run(repo_root, ["status", "--porcelain", "-u", "--"])
    except subprocess.CalledProcessError:
        return []
    dirty_files = []
    for line in result.stdout.splitlines():
        if len(line) > 3:
            # porcelain format: XY <path> or XY <old> -> <new>
            fpath = line[3:].split(" -> ")[-1].strip()
            if fpath.startswith('"') and fpath.endswith('"'):
                # Git C-quotes: octal escapes represent raw bytes (usually UTF-8).
                try:
                    fpath = (
                        fpath[1:-1]
                        .encode("utf-8")
                        .decode("unicode_escape")
                        .encode("latin-1")
                        .decode("utf-8")
                    )
                except (UnicodeDecodeError, UnicodeEncodeError):
                    fpath = fpath[1:-1]
            dirty_files.append(_to_posix(fpath))
    dirty: List[str] = []
    for cpath in component_paths:
        cpath_norm = cpath.rstrip("/")
        prefix = f"{cpath_norm}/"
        if any(f == cpath_norm or f.startswith(prefix) for f in dirty_files):
            dirty.append(cpath)
    return sorted(dirty)
