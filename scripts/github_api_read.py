#!/usr/bin/env python3
"""Run one idempotent GitHub API read with bounded transient retries."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


MAX_ATTEMPTS = 3
ATTEMPT_TIMEOUT_SECONDS = 30
BACKOFF_SECONDS = (0.25, 1.0)
MAX_OUTPUT_BYTES = 128 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_LABEL_CHARS = 256
CHUNK_BYTES = 16 * 1024

OK = 0
FAILED = 1
USAGE = 2

_HTTP_STATUS = re.compile(r"\bHTTP(?: status)?\s+([1-5][0-9]{2})\b", re.IGNORECASE)
_TRANSIENT_PHRASES = (
    "connection reset",
    "connection refused",
    "connection aborted",
    "context deadline exceeded",
    "could not resolve host",
    "dial tcp",
    "i/o timeout",
    "network is unreachable",
    "server closed idle connection",
    "temporary failure in name resolution",
    "tls handshake timeout",
)


class GitHubApiReadError(ValueError):
    """The requested command is not a safe bounded GitHub API read."""


@dataclass(frozen=True)
class AttemptResult:
    returncode: int
    stderr: bytes
    timed_out: bool = False
    output_exceeded: bool = False
    stderr_exceeded: bool = False


def _validate_label(label: object) -> str:
    if (
        type(label) is not str
        or not label
        or len(label) > MAX_LABEL_CHARS
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in label)
    ):
        raise GitHubApiReadError("API read label is invalid")
    return label


def _field_value(command: Sequence[str], names: frozenset[str]) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(command):
        argument = command[index]
        if argument in names:
            if index + 1 >= len(command):
                raise GitHubApiReadError(f"{argument} has no value")
            values.append(command[index + 1])
            index += 2
            continue
        for name in names:
            prefix = name + "="
            if argument.startswith(prefix):
                values.append(argument[len(prefix) :])
                break
        index += 1
    return values


def validate_read_command(command: Sequence[str]) -> tuple[str, ...]:
    command = tuple(command)
    if len(command) < 3:
        raise GitHubApiReadError("GitHub API read command is incomplete")
    executable = Path(command[0]).name.lower()
    if executable not in {"gh", "gh.exe"} or command[1] != "api":
        raise GitHubApiReadError("only gh api reads may be retried")

    forbidden = frozenset({"--input", "--method", "-X"})
    for argument in command[2:]:
        if argument in forbidden or any(
            argument.startswith(name + "=") for name in forbidden
        ):
            raise GitHubApiReadError("GitHub API mutations may not be retried")

    endpoints = [
        argument
        for argument in command[2:]
        if argument == "graphql" or argument.startswith("repos/")
    ]
    if len(endpoints) != 1:
        raise GitHubApiReadError("GitHub API read must identify one endpoint")
    endpoint = endpoints[0]

    fields = _field_value(
        command[2:],
        frozenset({"-f", "-F", "--field", "--raw-field"}),
    )
    if fields and endpoint != "graphql":
        raise GitHubApiReadError("REST field submission is not an idempotent read")
    if endpoint == "graphql":
        queries = [value[6:] for value in fields if value.startswith("query=")]
        if len(queries) != 1 or re.match(r"\s*query(?:\s|\(|\{)", queries[0]) is None:
            raise GitHubApiReadError("GraphQL retries require one read-only query")
        if re.search(r"\bmutation\b", queries[0], re.IGNORECASE):
            raise GitHubApiReadError("GraphQL mutations may not be retried")
    return command


def _bounded_stdout_reader(
    stream,
    target,
    limit: int,
    process: subprocess.Popen[bytes],
    state: dict[str, object],
) -> None:
    written = 0
    try:
        while True:
            chunk = stream.read(CHUNK_BYTES)
            if not chunk:
                break
            keep = max(0, min(len(chunk), limit + 1 - written))
            if keep:
                target.write(chunk[:keep])
                written += keep
            if keep < len(chunk) or written > limit:
                state["exceeded"] = True
                process.kill()
                break
    except (OSError, ValueError) as exc:
        state["error"] = exc


def _bounded_stderr_reader(
    stream,
    limit: int,
    process: subprocess.Popen[bytes],
    state: dict[str, object],
) -> None:
    captured = bytearray()
    try:
        while True:
            chunk = stream.read(CHUNK_BYTES)
            if not chunk:
                break
            keep = max(0, min(len(chunk), limit + 1 - len(captured)))
            if keep:
                captured.extend(chunk[:keep])
            if keep < len(chunk) or len(captured) > limit:
                state["exceeded"] = True
                process.kill()
                break
    except (OSError, ValueError) as exc:
        state["error"] = exc
    finally:
        state["captured"] = bytes(captured)


def run_once(
    command: Sequence[str],
    target: Path,
    output_limit: int,
    *,
    timeout: int = ATTEMPT_TIMEOUT_SECONDS,
) -> AttemptResult:
    try:
        process = subprocess.Popen(
            tuple(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
    except OSError as exc:
        raise GitHubApiReadError("could not start gh api") from exc
    assert process.stdout is not None and process.stderr is not None

    stdout_state: dict[str, object] = {}
    stderr_state: dict[str, object] = {}
    timed_out = False
    with target.open("wb") as output:
        stdout_thread = threading.Thread(
            target=_bounded_stdout_reader,
            args=(process.stdout, output, output_limit, process, stdout_state),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_bounded_stderr_reader,
            args=(process.stderr, MAX_STDERR_BYTES, process, stderr_state),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()
        stdout_thread.join()
        stderr_thread.join()

    for state in (stdout_state, stderr_state):
        error = state.get("error")
        if isinstance(error, BaseException):
            raise GitHubApiReadError("bounded API capture failed") from error
    return AttemptResult(
        process.returncode,
        stderr_state.get("captured", b""),
        timed_out=timed_out,
        output_exceeded=stdout_state.get("exceeded") is True,
        stderr_exceeded=stderr_state.get("exceeded") is True,
    )


def retryable_failure(result: AttemptResult) -> tuple[bool, str]:
    if result.timed_out:
        return True, "transport timeout"
    text = result.stderr.decode("utf-8", "replace")
    statuses = [int(value) for value in _HTTP_STATUS.findall(text)]
    if statuses:
        status = statuses[-1]
        return status in {408, 429} or status >= 500, f"HTTP {status}"
    lowered = text.lower()
    if any(phrase in lowered for phrase in _TRANSIENT_PHRASES):
        return True, "transport failure"
    return False, "non-transient API failure"


Runner = Callable[[Sequence[str], Path, int], AttemptResult]


def _discard_target(target: Path) -> None:
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


def run_with_retries(
    command: Sequence[str],
    target: Path,
    output_limit: int,
    label: str,
    *,
    runner: Runner = run_once,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    command = validate_read_command(command)
    label = _validate_label(label)
    if (
        type(output_limit) is not int
        or not 0 < output_limit <= MAX_OUTPUT_BYTES
    ):
        raise GitHubApiReadError("API output limit is invalid")
    if not target.parent.is_dir() or target.is_symlink():
        raise GitHubApiReadError("API output path is unsafe")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        # A failed response must never be combined with the next attempt.
        try:
            target.write_bytes(b"")
            result = runner(command, target, output_limit)
        except GitHubApiReadError:
            _discard_target(target)
            raise
        except OSError as exc:
            _discard_target(target)
            raise GitHubApiReadError("could not capture GitHub API output") from exc
        if result.output_exceeded:
            _discard_target(target)
            raise GitHubApiReadError(
                f"{label} exceeded the {output_limit}-byte safety limit"
            )
        if result.stderr_exceeded:
            _discard_target(target)
            raise GitHubApiReadError("GitHub API diagnostics exceeded the safety limit")
        if result.returncode == 0 and not result.timed_out:
            return OK

        retryable, reason = retryable_failure(result)
        if not retryable or attempt == MAX_ATTEMPTS:
            _discard_target(target)
            print(
                f"GitHub API read failed for {label} "
                f"(attempt {attempt}/{MAX_ATTEMPTS}, {reason}).",
                file=sys.stderr,
            )
            return FAILED
        print(
            f"Transient GitHub API read failure for {label} "
            f"(attempt {attempt}/{MAX_ATTEMPTS}, {reason}); retrying.",
            file=sys.stderr,
        )
        sleep(BACKOFF_SECONDS[attempt - 1])
    raise AssertionError("retry loop did not return")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--limit", required=True, type=int)
    parser.add_argument("--label", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = list(args.command)
    if command[:1] == ["--"]:
        command.pop(0)
    try:
        return run_with_retries(command, args.output, args.limit, args.label)
    except GitHubApiReadError as exc:
        _discard_target(args.output)
        print(f"ERROR: {exc}", file=sys.stderr)
        return USAGE


if __name__ == "__main__":
    raise SystemExit(main())
