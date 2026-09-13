"""Ask real Git what the answer is.

A dozen obligations in the register are of the form "boundver must agree with
Git": the mode it records for a file, whether a path is ignored, which blob id
content hashes to, which paths a diff changed. Git is already a hard runtime
dependency, so the oracle costs nothing to obtain and is the only authority
that settles those questions. Reimplementing the rule in the test would only
restate the code under test.

Every function here shells out and returns what Git said, with no
interpretation. Where Git's rule is short enough to state exactly rather than
run, `git_mode_for_permissions` states it, so a test can run on a host with no
POSIX permission bits at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

NUL = bytes([0])


def _git(root: Path, *args: str, check: bool = True) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, check=False
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return result.stdout


def _split_nul(payload: bytes) -> List[str]:
    return [
        field.decode("utf-8", "surrogateescape")
        for field in payload.split(NUL)
        if field
    ]


def index_modes(root: Path) -> Dict[str, str]:
    """The six-digit mode Git recorded in the index, per path."""
    modes: Dict[str, str] = {}
    for record in _split_nul(_git(root, "ls-files", "--stage", "-z")):
        metadata, path = record.split("\t", 1)
        mode = metadata.split(" ", 1)[0]
        modes[path] = mode
    return modes


def head_modes(root: Path, revision: str = "HEAD") -> Dict[str, str]:
    """The same, as recorded in a commit's tree."""
    modes: Dict[str, str] = {}
    for record in _split_nul(_git(root, "ls-tree", "-r", "-z", revision)):
        metadata, path = record.split("\t", 1)
        mode = metadata.split(" ", 1)[0]
        modes[path] = mode
    return modes


def git_mode_for_permissions(permissions: int) -> str:
    """Git's rule for a regular file, stated rather than run.

    Git derives the mode from the owner execute bit alone. `ce_mode_from_stat`
    in read-cache.c reduces a regular file to ``0755`` when ``mode & 0100`` is
    set and ``0644`` otherwise, so group and other execute never participate.
    Stating it here lets a test check the classifier on a host that has no
    permission bits to set.
    """
    return "100755" if permissions & 0o100 else "100644"


def ignored_paths(root: Path, candidates: Iterable[str]) -> Set[str]:
    """Which of *candidates* Git's exclude rules would exclude.

    ``--no-index`` keeps the question about the ignore rules alone, which is
    what `_GitignoreRules` models: without it Git reports a tracked path as
    not-excluded no matter what the patterns say. Exit status 1 means nothing
    matched and is not an error.
    """
    payload = NUL.join(
        candidate.encode("utf-8", "surrogateescape") for candidate in candidates
    )
    if not payload:
        return set()
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-z", "--stdin"],
        cwd=root,
        input=payload,
        capture_output=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise AssertionError(
            "git check-ignore failed "
            f"({result.returncode}): {result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return set(_split_nul(result.stdout))


def untracked_paths(root: Path) -> Set[str]:
    """The corpus Git itself would offer as untracked and not excluded."""
    return set(
        _split_nul(
            _git(root, "ls-files", "--others", "--exclude-standard", "-z")
        )
    )


def tracked_paths(root: Path) -> Set[str]:
    return set(_split_nul(_git(root, "ls-files", "-z")))


def blob_id(root: Path, path: str) -> str:
    """The object id Git would store for the working-tree file at *path*."""
    return _git(root, "hash-object", "--", path).decode("ascii").strip()


def blob_id_for_bytes(root: Path, payload: bytes) -> str:
    result = subprocess.run(
        ["git", "hash-object", "--stdin"],
        cwd=root,
        input=payload,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode("ascii").strip()


def name_status(
    root: Path,
    old: str,
    new: Optional[str] = None,
    *,
    extra: Sequence[str] = (),
) -> List[Tuple[str, str]]:
    """``git diff --name-status -z`` as (status letter, path) pairs.

    A rename or copy record spans three NUL-separated fields, so the stream
    cannot be read as simple pairs.
    """
    arguments = ["diff", "--name-status", "-z", *extra, old]
    if new is not None:
        arguments.append(new)
    fields = _split_nul(_git(root, *arguments))
    entries: List[Tuple[str, str]] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        if status[:1] in {"R", "C"}:
            entries.append((status, fields[index + 1]))
            entries.append((status, fields[index + 2]))
            index += 3
        else:
            entries.append((status, fields[index + 1]))
            index += 2
    return entries
