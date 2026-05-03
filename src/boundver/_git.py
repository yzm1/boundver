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
    return result.stdout


def _git_batch_cat(repo_root: Path, refs: List[str]) -> Dict[str, bytes]:
    """Batch-read multiple git objects via ``git cat-file --batch``.

    All objects are fetched in a single subprocess, replacing O(N) ``git show``
    calls with O(1).  Returns ``{ref: raw_bytes}``.  Missing objects map to
    ``b""``.  Raises ``subprocess.CalledProcessError`` on git failure.
    """
    if not refs:
        return {}
    inp = "\n".join(refs) + "\n"
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
    for ref in refs:
        nl = data.index(b"\n", pos)
        header = data[pos:nl].decode("ascii", errors="replace")
        pos = nl + 1
        parts = header.split()
        if len(parts) >= 2 and parts[1] == "missing":
            blobs[ref] = b""
            continue
        if len(parts) < 3:
            blobs[ref] = b""
            continue
        size = int(parts[2])
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


def _load_gitignore_patterns(repo_root: Path) -> Optional[List[str]]:
    """Parse .gitignore into a list of bare patterns (no negation support)."""
    gi = repo_root / ".gitignore"
    if not gi.exists():
        return None
    patterns: List[str] = []
    for line in gi.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            continue  # negation patterns not supported in fallback
        patterns.append(line.rstrip("/"))
    return patterns


def _matches_gitignore(rel_path: str, patterns: List[str]) -> bool:
    """Check if a repo-relative path matches any gitignore pattern."""
    import fnmatch
    parts = rel_path.replace("\\", "/").split("/")
    for pattern in patterns:
        # Pattern without / matches any path component
        if "/" not in pattern:
            if any(fnmatch.fnmatch(part, pattern) for part in parts):
                return True
        else:
            # Pattern with / — match as prefix (directory) or full glob from root
            if rel_path.startswith(pattern + "/") or rel_path == pattern:
                return True
            if fnmatch.fnmatch(rel_path, pattern):
                return True
    return False


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


def _head_entries_for_path(repo_root: Path, base_path: str) -> List[str]:
    """List file entries for a repo-relative path at HEAD.

    If `base_path` is a file in HEAD, returns one entry.
    If it's a directory, returns all descendant files.
    """
    base = base_path.rstrip("/")
    files = list_head_files(repo_root, base)
    if files:
        return files
    return []


def _list_files_for_source(repo_root: Path, repo_rel_path: str, source: str) -> List[str]:
    if source == "head":
        return list_head_files(repo_root, repo_rel_path)
    args = ["ls-files"]
    if source == "index":
        args.append("--cached")
    args.extend(["--", repo_rel_path])
    try:
        result = _git_run(repo_root, args)
    except subprocess.CalledProcessError:
        result_files: List[str] = []
    else:
        result_files = [_to_posix(line.strip()) for line in result.stdout.splitlines() if line.strip()]

    if result_files:
        return result_files

    # Fallback for non-git test/runtime environments: local filesystem enumeration.
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
        if gitignore_patterns is not None:
            if _matches_gitignore(rel, gitignore_patterns):
                continue
        elif _is_ignored(f):
            continue
        results.append(rel)
    return results


def git_latest_tag(repo_root: Path, prefix: str) -> Optional[str]:
    """Find the latest reachable git tag matching a prefix, extract version part."""
    try:
        # Prefer reachable tags from current HEAD to avoid unrelated branch tags.
        result = _git_run(repo_root, ["describe", "--tags", "--match", f"{prefix}*", "--abbrev=0"])
        tag = result.stdout.strip()
        return tag[len(prefix):] if tag.startswith(prefix) else None
    except subprocess.CalledProcessError:
        try:
            # Fallback for repos where describe cannot resolve (e.g. shallow/no reachable matches).
            result = _git_run(repo_root, ["tag", "--list", f"{prefix}*", "--sort=-v:refname"])
            tags = [t for t in result.stdout.strip().split("\n") if t]
            if tags:
                return tags[0][len(prefix):]
            return None
        except subprocess.CalledProcessError:
            return None


def changed_components_since_ref(config: dict, repo_root: Path, base_ref: str) -> List[str]:
    """Return component names with tracked changes since `base_ref`."""
    try:
        result = _git_run(repo_root, ["diff", "--name-only", base_ref, "--"])
    except subprocess.CalledProcessError:
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
