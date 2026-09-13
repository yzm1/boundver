"""Tests for bounded idempotent GitHub API retries."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "github_api_read.py"
SPEC = importlib.util.spec_from_file_location("boundver_github_api_read", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
api_read = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = api_read
SPEC.loader.exec_module(api_read)

REST_READ = ("gh", "api", "repos/yzm1/boundver")
GRAPHQL_READ = (
    "gh",
    "api",
    "graphql",
    "-f",
    "query=query($owner:String!){repository(owner:$owner){id}}",
    "-F",
    "owner=yzm1",
)


def _result(
    returncode: int,
    stderr: bytes = b"",
    *,
    timed_out: bool = False,
    output_exceeded: bool = False,
    stderr_exceeded: bool = False,
):
    return api_read.AttemptResult(
        returncode,
        stderr,
        timed_out=timed_out,
        output_exceeded=output_exceeded,
        stderr_exceeded=stderr_exceeded,
    )


def test_transient_transport_failure_retries_with_a_fresh_output(
    tmp_path: Path,
    capsys,
) -> None:
    target = tmp_path / "capture"
    attempts = 0

    def runner(command, output, limit):
        nonlocal attempts
        assert command == REST_READ
        assert limit == 1024
        assert output.read_bytes() == b""
        attempts += 1
        if attempts == 1:
            output.write_bytes(b"partial")
            return _result(1, b'dial tcp 20.0.0.1:443: i/o timeout')
        output.write_bytes(b"complete")
        return _result(0)

    result = api_read.run_with_retries(
        REST_READ,
        target,
        1024,
        "repository metadata",
        runner=runner,
        sleep=lambda _delay: None,
    )

    assert result == 0
    assert attempts == 2
    assert target.read_bytes() == b"complete"
    diagnostic = capsys.readouterr().err
    assert "attempt 1/3" in diagnostic
    assert "repository metadata" in diagnostic
    assert "partial" not in diagnostic


def test_permanent_transport_failure_exhausts_the_fixed_attempts(
    tmp_path: Path,
    capsys,
) -> None:
    target = tmp_path / "capture"
    attempts = 0

    def runner(_command, output, _limit):
        nonlocal attempts
        attempts += 1
        output.write_bytes(f"partial-{attempts}".encode())
        return _result(1, b"connection reset by peer")

    result = api_read.run_with_retries(
        REST_READ,
        target,
        1024,
        "reviews",
        runner=runner,
        sleep=lambda _delay: None,
    )

    assert result == 1
    assert attempts == api_read.MAX_ATTEMPTS
    assert not target.exists()
    diagnostic = capsys.readouterr().err
    assert "attempt 3/3" in diagnostic
    assert "partial-" not in diagnostic


@pytest.mark.parametrize("status", [401, 403, 404])
def test_authorization_and_not_found_responses_are_not_retried(
    tmp_path: Path,
    status: int,
) -> None:
    target = tmp_path / "capture"
    attempts = 0

    def runner(_command, _output, _limit):
        nonlocal attempts
        attempts += 1
        return _result(1, f"gh: request failed (HTTP {status})".encode())

    result = api_read.run_with_retries(
        REST_READ,
        target,
        1024,
        "repository metadata",
        runner=runner,
        sleep=lambda _delay: None,
    )

    assert result == 1
    assert attempts == 1
    assert not target.exists()


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
def test_transient_http_responses_are_bounded_retries(
    tmp_path: Path,
    status: int,
) -> None:
    target = tmp_path / "capture"
    attempts = 0

    def runner(_command, output, _limit):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _result(1, f"gh: unavailable (HTTP {status})".encode())
        output.write_bytes(b"ok")
        return _result(0)

    assert (
        api_read.run_with_retries(
            REST_READ,
            target,
            1024,
            "repository metadata",
            runner=runner,
            sleep=lambda _delay: None,
        )
        == 0
    )
    assert attempts == 2


def test_output_limit_is_enforced_by_the_streaming_capture(tmp_path: Path) -> None:
    target = tmp_path / "capture"
    command = (
        sys.executable,
        "-I",
        "-c",
        "import sys; sys.stdout.buffer.write(b'x' * 100000)",
    )

    result = api_read.run_once(command, target, 32, timeout=5)

    assert result.output_exceeded is True
    assert target.stat().st_size <= 33


def test_output_overflow_is_not_retried_or_left_on_disk(
    tmp_path: Path,
) -> None:
    target = tmp_path / "capture"
    attempts = 0

    def runner(_command, output, _limit):
        nonlocal attempts
        attempts += 1
        output.write_bytes(b"x" * 33)
        return _result(-9, output_exceeded=True)

    with pytest.raises(api_read.GitHubApiReadError, match="safety limit"):
        api_read.run_with_retries(
            REST_READ,
            target,
            32,
            "reviews",
            runner=runner,
            sleep=lambda _delay: None,
        )

    assert attempts == 1
    assert not target.exists()


@pytest.mark.parametrize(
    "command",
    [
        ("curl", "api", "repos/yzm1/boundver"),
        ("gh", "api", "--method", "POST", "repos/yzm1/boundver"),
        ("gh", "api", "repos/yzm1/boundver", "-f", "state=closed"),
        ("gh", "api", "graphql", "-f", "query=mutation { deleteProject }"),
        ("gh", "api", "graphql", "-F", "owner=yzm1"),
    ],
)
def test_only_idempotent_rest_and_graphql_reads_are_accepted(command) -> None:
    with pytest.raises(api_read.GitHubApiReadError):
        api_read.validate_read_command(command)

    assert api_read.validate_read_command(REST_READ) == REST_READ
    assert api_read.validate_read_command(GRAPHQL_READ) == GRAPHQL_READ


def test_subprocess_diagnostics_are_bounded(tmp_path: Path) -> None:
    target = tmp_path / "capture"
    command = (
        sys.executable,
        "-I",
        "-c",
        "import sys; sys.stderr.buffer.write(b'x' * 100000)",
    )

    result = api_read.run_once(command, target, 32, timeout=5)

    assert result.stderr_exceeded is True
    assert len(result.stderr) <= api_read.MAX_STDERR_BYTES + 1


def test_capture_path_errors_return_a_bounded_usage_failure(
    tmp_path: Path,
    capsys,
) -> None:
    target = tmp_path / "directory"
    target.mkdir()

    result = api_read.main(
        [
            "--output",
            str(target),
            "--limit",
            "32",
            "--label",
            "repository metadata",
            "--",
            *REST_READ,
        ]
    )

    assert result == api_read.USAGE
    assert "could not capture GitHub API output" in capsys.readouterr().err


def test_release_review_audit_routes_every_api_call_through_the_retry_helper() -> None:
    script = (ROOT / "scripts" / "audit_release_reviews.sh").read_text(
        encoding="utf-8"
    )

    assert script.count("run_github_api_bounded_to_file") == 5
    assert script.count("capture_github_api_bounded") == 10
    for line in script.splitlines():
        if "gh api" in line:
            assert "run_bounded_to_file" not in line
            assert "capture_bounded" not in line
