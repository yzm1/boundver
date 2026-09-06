#!/usr/bin/env python3
"""Run the shared, read-only release-candidate verification sequence.

Dependency installation and review auditing intentionally remain outside this
program.  Callers can therefore give this verifier a credential-free
environment and keep trusted API access in its own narrowly scoped step.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Optional, Sequence


def _load_release_platform():
    """Load the exact adjacent helper even under isolated Python startup."""
    path = Path(__file__).resolve().with_name("_release_platform.py")
    spec = importlib.util.spec_from_file_location(
        "_boundver_release_platform", path
    )
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise RuntimeError(f"cannot load release platform helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_release_platform = _load_release_platform()
resolve_bash = _release_platform.resolve_bash
sanitize_git_environment = _release_platform.sanitize_git_environment
sanitize_shell_environment = _release_platform.sanitize_shell_environment


TAG_RE = re.compile(
    r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
)
SHA_RE = re.compile(r"[0-9a-f]{40}")
MAX_DIST_ENTRIES = 64
MAX_DIST_NAME_BYTES = 4 * 1024
MAX_DIST_TOTAL_NAME_BYTES = 64 * 1024
MAX_COMMAND_SECONDS = 3_600
MAX_CAPTURED_OUTPUT_CHARS = 64 * 1024


class CandidateVerificationError(RuntimeError):
    """The candidate could not be proved ready for publication."""


def _is_windows_reparse_point(identity: os.stat_result) -> bool:
    attributes = getattr(identity, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _changed(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or before.st_mode != after.st_mode
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    )


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            cwd=cwd,
            env=dict(env),
            text=True,
            capture_output=capture_output,
            check=True,
            stdin=subprocess.DEVNULL,
            timeout=MAX_COMMAND_SECONDS,
        )
    except FileNotFoundError as error:
        raise CandidateVerificationError(
            f"required command is unavailable: {command[0]}"
        ) from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()[
            :MAX_CAPTURED_OUTPUT_CHARS
        ]
        suffix = f": {detail}" if detail else f" (exit {error.returncode})"
        raise CandidateVerificationError(
            f"{' '.join(command)} failed{suffix}"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise CandidateVerificationError(
            f"{' '.join(command)} timed out"
        ) from error


def _trusted_tool(command: str, repo: Path, search_path: Optional[str]) -> str:
    """Resolve one executable and reject repository-local selections."""
    selected = (
        command
        if Path(command).is_absolute()
        else shutil.which(command, path=search_path)
    )
    if not selected:
        raise CandidateVerificationError(
            f"required command is unavailable: {command}"
        )
    try:
        root = repo.resolve(strict=True)
        raw = Path(os.path.abspath(selected))
        resolved = Path(selected).resolve(strict=True)
        identity = resolved.stat()
    except OSError as error:
        raise CandidateVerificationError(
            f"required command is unavailable: {command}"
        ) from error
    if (
        not stat.S_ISREG(identity.st_mode)
        or raw == root
        or root in raw.parents
        or resolved == root
        or root in resolved.parents
    ):
        raise CandidateVerificationError(
            f"refusing executable selected from the release repository: {command}"
        )
    return str(resolved)


def _git_environment(environment: Mapping[str, str]) -> dict[str, str]:
    result = sanitize_git_environment(environment)
    result.update(
        {
            "GCM_INTERACTIVE": "Never",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_KEY_1": "core.fsmonitor",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_VALUE_0": os.devnull,
            "GIT_CONFIG_VALUE_1": "false",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return result


def _git_output(
    repo: Path,
    git: str,
    arguments: Sequence[str],
    environment: Mapping[str, str],
) -> str:
    result = _run(
        (
            git,
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            "--no-optional-locks",
            *arguments,
        ),
        cwd=repo,
        env=_git_environment(environment),
        capture_output=True,
    )
    if (
        len(result.stdout) > MAX_CAPTURED_OUTPUT_CHARS
        or len(result.stderr) > MAX_CAPTURED_OUTPUT_CHARS
    ):
        raise CandidateVerificationError("Git command output exceeds its limit")
    return result.stdout.strip()


def _release_distributions(repo: Path) -> tuple[Path, Path]:
    distribution_dir = repo / "dist"
    try:
        before = distribution_dir.lstat()
        if (
            not stat.S_ISDIR(before.st_mode)
            or _is_windows_reparse_point(before)
        ):
            raise CandidateVerificationError(
                "release distribution path is not a plain directory"
            )
        wheels: list[tuple[Path, os.stat_result]] = []
        sdists: list[tuple[Path, os.stat_result]] = []
        entry_count = 0
        total_name_bytes = 0
        with os.scandir(distribution_dir) as entries:
            for entry in entries:
                entry_count += 1
                if entry_count > MAX_DIST_ENTRIES:
                    raise CandidateVerificationError(
                        "release distribution directory exceeds the "
                        f"{MAX_DIST_ENTRIES}-entry limit"
                    )
                name_bytes = len(
                    entry.name.encode("utf-8", "surrogatepass")
                )
                if name_bytes > MAX_DIST_NAME_BYTES:
                    raise CandidateVerificationError(
                        "release distribution name exceeds the "
                        f"{MAX_DIST_NAME_BYTES}-byte limit"
                    )
                total_name_bytes += name_bytes
                if total_name_bytes > MAX_DIST_TOTAL_NAME_BYTES:
                    raise CandidateVerificationError(
                        "release distribution names exceed the "
                        f"{MAX_DIST_TOTAL_NAME_BYTES}-byte aggregate limit"
                    )
                path = Path(entry.path)
                identity = path.lstat()
                if (
                    not stat.S_ISREG(identity.st_mode)
                    or _is_windows_reparse_point(identity)
                ):
                    raise CandidateVerificationError(
                        "release distribution directory contains a symlink, "
                        f"reparse point, or non-regular entry: {entry.name}"
                    )
                if entry.name.endswith(".whl"):
                    wheels.append((path, identity))
                elif entry.name.endswith(".tar.gz"):
                    sdists.append((path, identity))
        after = distribution_dir.lstat()
    except FileNotFoundError as error:
        raise CandidateVerificationError(
            f"release distribution directory does not exist: {distribution_dir}"
        ) from error
    except OSError as error:
        raise CandidateVerificationError(
            f"cannot inspect release distribution directory: {error}"
        ) from error
    if (
        not stat.S_ISDIR(after.st_mode)
        or _is_windows_reparse_point(after)
        or _changed(before, after)
    ):
        raise CandidateVerificationError(
            "release distribution directory changed while being inspected"
        )
    if len(wheels) != 1 or len(sdists) != 1:
        raise CandidateVerificationError(
            "packaging smoke must create exactly one wheel and one source distribution"
        )
    results: list[Path] = []
    for path, before_file in (wheels[0], sdists[0]):
        try:
            after_file = path.lstat()
        except OSError as error:
            raise CandidateVerificationError(
                f"release distribution changed while being inspected: {path.name}"
            ) from error
        if (
            not stat.S_ISREG(after_file.st_mode)
            or _is_windows_reparse_point(after_file)
            or _changed(before_file, after_file)
        ):
            raise CandidateVerificationError(
                f"release distribution changed while being inspected: {path.name}"
            )
        results.append(path)
    return results[0], results[1]


def _packaging_bash(
    search_path: Optional[str],
    *,
    platform_name: str = os.name,
    forbidden_root: Optional[Path] = None,
) -> Optional[str]:
    """Compatibility wrapper around the shared release-tool resolver."""
    return resolve_bash(
        search_path,
        platform_name=platform_name,
        forbidden_root=forbidden_root,
    )


def verify_candidate(
    repo: Path,
    tag: str,
    release_sha: str,
    *,
    python: str = sys.executable,
    environment: Optional[Mapping[str, str]] = None,
) -> tuple[Path, Path]:
    """Verify readiness, tests, reproducible packages, and package metadata."""
    repo = repo.resolve()
    if not repo.is_dir():
        raise CandidateVerificationError(f"repository does not exist: {repo}")
    if TAG_RE.fullmatch(tag) is None:
        raise CandidateVerificationError("tag must be an exact vMAJOR.MINOR.PATCH")
    if SHA_RE.fullmatch(release_sha) is None:
        raise CandidateVerificationError(
            "release SHA must be an exact lowercase 40-character commit ID"
        )

    tool_env = sanitize_shell_environment(environment)
    interpreter_dir = str(Path(python).resolve().parent)
    existing_path = tool_env.get("PATH")
    tool_env["PATH"] = (
        interpreter_dir + os.pathsep + existing_path
        if existing_path
        else interpreter_dir
    )

    python = _trusted_tool(python, repo, tool_env.get("PATH"))
    git = _trusted_tool("git", repo, tool_env.get("PATH"))

    head = _git_output(
        repo, git, ("rev-parse", "--verify", "HEAD"), tool_env
    )
    if head != release_sha:
        raise CandidateVerificationError(
            f"checked-out commit {head!r} does not match release SHA {release_sha}"
        )
    epoch = _git_output(
        repo,
        git,
        ("show", "-s", "--format=%ct", release_sha),
        tool_env,
    )
    if not epoch.isascii() or not epoch.isdecimal():
        raise CandidateVerificationError("release commit timestamp is not an integer")

    _run(
        (python, "-I", "scripts/verify_release_readiness.py", "--tag", tag),
        cwd=repo,
        env=tool_env,
    )
    _run((python, "-I", "-m", "pytest", "-q"), cwd=repo, env=tool_env)
    _run(
        (python, "-I", "scripts/demo_consumer_impact.py"),
        cwd=repo,
        env=tool_env,
    )
    _run(
        (python, "-I", "scripts/demo_range_review.py"),
        cwd=repo,
        env=tool_env,
    )

    bash = _packaging_bash(tool_env.get("PATH"), forbidden_root=repo)
    if bash is None:
        raise CandidateVerificationError(
            "bash is required by scripts/packaging_smoke.sh but is unavailable"
        )
    build_env = tool_env.copy()
    build_env["SOURCE_DATE_EPOCH"] = epoch
    _run(
        (bash, "scripts/packaging_smoke.sh"),
        cwd=repo,
        env=build_env,
    )

    wheel, sdist = _release_distributions(repo)
    _run(
        (python, "-I", "-m", "twine", "check", str(wheel), str(sdist)),
        cwd=repo,
        env=tool_env,
    )
    return wheel, sdist


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a checked-out boundver release candidate."
    )
    parser.add_argument("--tag", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--repo", type=Path, default=Path("."))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        wheel, sdist = verify_candidate(args.repo, args.tag, args.release_sha)
    except CandidateVerificationError as error:
        print(f"release candidate verification failed: {error}", file=sys.stderr)
        return 1
    print(
        "Release candidate verified: "
        f"{args.tag} ({args.release_sha}); {wheel.name}; {sdist.name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
