#!/usr/bin/env python3
"""Validate one monotonic compatibility-alias move without mutating refs."""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Iterable, Sequence


def _load_release_platform():
    """Load the exact adjacent helper even under isolated Python startup."""
    path = Path(__file__).resolve().with_name("_release_platform.py")
    spec = importlib.util.spec_from_file_location(
        "_boundver_alias_validation_release_platform", path
    )
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise RuntimeError(f"cannot load release platform helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sanitize_git_environment = _load_release_platform().sanitize_git_environment


MAX_TAG_LIST_BYTES = 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_TAG_RECORDS = 10_000
MAX_TAG_LINE_BYTES = 8192
MAX_VERSION_DIGITS = 640
READ_CHUNK_BYTES = 64 * 1024
SHA_RE = re.compile(r"[0-9a-f]{40}")
TAG_RE = re.compile(
    r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
)
ALIAS_RE = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
REMOTE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class AliasValidationError(RuntimeError):
    """The proposed alias move is malformed, unsafe, or a rollback."""


def _kill(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except OSError:
        pass


def _read_pipe(
    process: subprocess.Popen[bytes],
    pipe,
    limit: int,
    output: bytearray,
    errors: list[str],
) -> None:
    try:
        while True:
            remaining_with_sentinel = max(1, limit - len(output) + 1)
            reader = getattr(pipe, "read1", pipe.read)
            chunk = reader(min(READ_CHUNK_BYTES, remaining_with_sentinel))
            if not chunk:
                return
            if not isinstance(chunk, bytes) or len(output) + len(chunk) > limit:
                errors.append("output exceeds its byte limit")
                _kill(process)
                return
            output.extend(chunk)
    except (OSError, ValueError):
        errors.append("output read failed")
        _kill(process)
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _run_git(
    git: Path,
    repo: Path,
    runner_temp: Path,
    arguments: Sequence[str],
    *,
    accepted_returncodes: Iterable[int] = (0,),
    stdout_limit: int = MAX_TAG_LIST_BYTES,
) -> tuple[int, bytes]:
    environment = sanitize_git_environment()
    environment.update(
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
    try:
        process = subprocess.Popen(
            [
                str(git),
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "core.fsmonitor=false",
                "--no-optional-locks",
                "-C",
                str(repo),
                *arguments,
            ],
            cwd=runner_temp,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise AliasValidationError("Git command could not start") from error
    if process.stdout is None or process.stderr is None:
        _kill(process)
        raise AliasValidationError("Git command pipes are unavailable")

    stdout = bytearray()
    stderr = bytearray()
    stdout_errors: list[str] = []
    stderr_errors: list[str] = []
    readers = (
        threading.Thread(
            target=_read_pipe,
            args=(process, process.stdout, stdout_limit, stdout, stdout_errors),
            daemon=True,
        ),
        threading.Thread(
            target=_read_pipe,
            args=(process, process.stderr, MAX_STDERR_BYTES, stderr, stderr_errors),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()
    try:
        returncode = process.wait(timeout=90)
    except subprocess.TimeoutExpired as error:
        _kill(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise AliasValidationError("Git command timed out") from error
    finally:
        for reader in readers:
            reader.join(timeout=5)
    if any(reader.is_alive() for reader in readers):
        _kill(process)
        raise AliasValidationError("Git command pipes did not close")
    if stdout_errors or stderr_errors:
        detail = (stdout_errors + stderr_errors)[0]
        raise AliasValidationError(f"Git command {detail}")
    if returncode not in set(accepted_returncodes):
        detail = stderr.decode("utf-8", "backslashreplace").strip()
        raise AliasValidationError(f"Git command failed: {detail}")
    return returncode, bytes(stdout)


def _records(payload: bytes) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for raw_line in payload.splitlines(keepends=True):
        if len(result) >= MAX_TAG_RECORDS or len(raw_line) > MAX_TAG_LINE_BYTES:
            raise AliasValidationError(
                "compatibility tag query exceeds the record limit"
            )
        try:
            line = raw_line.rstrip(b"\r\n").decode("utf-8", "strict")
        except UnicodeDecodeError as error:
            raise AliasValidationError(
                "compatibility tag query is not UTF-8"
            ) from error
        fields = line.split("\t")
        if len(fields) != 2 or SHA_RE.fullmatch(fields[0]) is None:
            raise AliasValidationError(
                "compatibility tag query returned a malformed record"
            )
        result.append((fields[0], fields[1]))
    return result


def _remote_ref(
    git: Path,
    repo: Path,
    runner_temp: Path,
    remote: str,
    ref: str,
) -> str:
    _returncode, payload = _run_git(
        git, repo, runner_temp, ("ls-remote", remote, ref)
    )
    records = _records(payload)
    if any(remote_ref != ref for _sha, remote_ref in records) or len(records) > 1:
        raise AliasValidationError(f"remote returned ambiguous ref state for {ref}")
    return records[0][0] if records else ""


def _append_outputs(
    output: Path,
    runner_temp: Path,
    *,
    update_required: bool,
    expected_current: str,
) -> None:
    resolved_output = output.resolve()
    resolved_temp = runner_temp.resolve()
    if (
        not output.is_file()
        or output.is_symlink()
        or resolved_output == resolved_temp
        or resolved_temp not in resolved_output.parents
    ):
        raise AliasValidationError("workflow output path is unsafe")
    try:
        with output.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                f"update-required={str(update_required).lower()}\n"
                f"expected-current={expected_current}\n"
            )
    except OSError as error:
        raise AliasValidationError("cannot write workflow outputs") from error


def validate_alias(
    *,
    release_tag: str,
    release_sha: str,
    alias: str,
    repo: Path,
    runner_temp: Path,
    output: Path,
    remote: str,
) -> None:
    release_match = TAG_RE.fullmatch(release_tag)
    alias_match = ALIAS_RE.fullmatch(alias)
    if (
        release_match is None
        or alias_match is None
        or SHA_RE.fullmatch(release_sha) is None
        or REMOTE_RE.fullmatch(remote) is None
    ):
        raise AliasValidationError("alias validation inputs are malformed")
    if any(
        len(group) > MAX_VERSION_DIGITS
        for group in (*release_match.groups(), *alias_match.groups())
    ):
        raise AliasValidationError("release version exceeds the digit limit")
    expected_alias = release_tag.rsplit(".", 1)[0]
    if alias != expected_alias:
        raise AliasValidationError(
            f"compatibility alias must be {expected_alias}, got {alias}"
        )
    patch_digits = release_match.group(3)
    release_patch = int(patch_digits)

    repo = repo.resolve()
    runner_temp = runner_temp.resolve()
    selected_git = shutil.which("git")
    try:
        raw_git = Path(os.path.abspath(selected_git or ""))
        git = Path(selected_git or "").resolve(strict=True)
        git_identity = git.stat()
    except OSError as error:
        raise AliasValidationError(
            "trusted Git executable or repository is unavailable"
        ) from error
    if (
        not repo.is_dir()
        or not runner_temp.is_dir()
        or not stat.S_ISREG(git_identity.st_mode)
        or raw_git == repo
        or repo in raw_git.parents
        or git == repo
        or repo in git.parents
    ):
        raise AliasValidationError("trusted Git executable or repository is unavailable")

    release_ref = f"refs/tags/{release_tag}"
    if _remote_ref(git, repo, runner_temp, remote, release_ref) != release_sha:
        raise AliasValidationError("exact release tag moved before alias update")

    alias_ref = f"refs/tags/{alias}"
    current = _remote_ref(git, repo, runner_temp, remote, alias_ref)
    if current == release_sha:
        _append_outputs(
            output,
            runner_temp,
            update_required=False,
            expected_current=current,
        )
        return

    if current:
        private_ref = "refs/boundver-release/compatibility-alias-before"
        _run_git(
            git,
            repo,
            runner_temp,
            ("fetch", "--no-tags", remote, f"{alias_ref}:{private_ref}"),
        )
        returncode, _payload = _run_git(
            git,
            repo,
            runner_temp,
            ("merge-base", "--is-ancestor", current, release_sha),
            accepted_returncodes=(0, 1),
            stdout_limit=1024,
        )
        if returncode != 0:
            raise AliasValidationError(
                f"refusing non-ancestral or rollback alias move: "
                f"{current} -> {release_sha}"
            )

        _returncode, payload = _run_git(
            git,
            repo,
            runner_temp,
            (
                "ls-remote",
                "--tags",
                "--refs",
                remote,
                f"refs/tags/{alias}.*",
            ),
        )
        patch_pattern = re.compile(
            r"refs/tags/" + re.escape(alias) + r"\.([0-9]+)"
        )
        current_patches: list[int] = []
        seen_refs: set[str] = set()
        for sha, ref in _records(payload):
            match = patch_pattern.fullmatch(ref)
            if match is None or ref in seen_refs:
                raise AliasValidationError(
                    "compatibility tag query returned an unexpected ref"
                )
            seen_refs.add(ref)
            digits = match.group(1)
            if len(digits) > MAX_VERSION_DIGITS:
                raise AliasValidationError(
                    "compatibility tag patch exceeds the digit limit"
                )
            if sha == current:
                current_patches.append(int(digits))
        if not current_patches:
            raise AliasValidationError(
                "existing compatibility alias does not point to an exact "
                "same-line release"
            )
        current_patch = max(current_patches)
        if current_patch >= release_patch:
            raise AliasValidationError(
                f"refusing compatibility alias rollback from patch "
                f"{current_patch} to patch {release_patch}"
            )

    if _remote_ref(git, repo, runner_temp, remote, release_ref) != release_sha:
        raise AliasValidationError(
            "exact release tag moved at the alias mutation boundary"
        )
    _append_outputs(
        output,
        runner_temp,
        update_required=True,
        expected_current=current,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--alias", required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--runner-temp", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    parser.add_argument("--remote", default="origin")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        validate_alias(
            release_tag=args.release_tag,
            release_sha=args.release_sha,
            alias=args.alias,
            repo=args.repo,
            runner_temp=args.runner_temp,
            output=args.github_output,
            remote=args.remote,
        )
    except AliasValidationError as error:
        print(f"Compatibility-alias validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
