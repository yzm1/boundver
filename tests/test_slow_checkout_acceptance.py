"""Slow-checkout acceptance through the public CLI and real Git watchdog."""

import json
import os
import subprocess
import sys

import pytest

from boundver import _git
from scripts.benchmark_adoption import create_fixture
from tests._parity import run_cli_in_process


@pytest.mark.skipif(os.name != "posix", reason="uses POSIX exec to delay the owned Git PID")
def test_slow_git_config_query_in_65_component_checkout(tmp_path, monkeypatch):
    create_fixture(tmp_path, 65)
    lock_path = tmp_path / "boundary.lock.json"
    lock_before = lock_path.read_bytes()
    index_path = tmp_path / ".git" / "index"
    index_before = index_path.read_bytes()
    original_popen = subprocess.Popen

    def validate_with_delayed_probe(seconds):
        probes = 0
        delayed = None

        def launch(command, *args, **kwargs):
            nonlocal probes, delayed
            if (
                isinstance(command, (list, tuple))
                and command
                and command[-1] == _git._FILTER_COMMAND_CONFIG_PATTERN
            ):
                probes += 1
                if probes == 1:
                    delayed = original_popen(
                        [
                            sys.executable, "-I", "-c",
                            "import os, sys, time; time.sleep(1.5); os.execv(sys.argv[1], sys.argv[1:])",
                            *command,
                        ],
                        *args, **kwargs,
                    )
                    return delayed
            return original_popen(command, *args, **kwargs)

        with monkeypatch.context() as patched:
            # Compress the default deadline for the fixture so the test
            # exercises a real watchdog without waiting ten seconds.
            patched.setattr(_git, "MAX_GIT_CONFIG_QUERY_SECONDS", 1)
            if seconds is None:
                patched.delenv("BOUNDVER_GIT_CONFIG_QUERY_TIMEOUT_SECONDS", raising=False)
            else:
                patched.setenv("BOUNDVER_GIT_CONFIG_QUERY_TIMEOUT_SECONDS", str(seconds))
            patched.setattr(_git.subprocess, "Popen", launch)
            result = run_cli_in_process(tmp_path, "validate-config")
        assert delayed is not None
        assert delayed.poll() is not None
        return result, probes

    expired, _ = validate_with_delayed_probe(None)
    assert expired.returncode == 2
    assert "CONFIG VALIDATION UNAVAILABLE [git-timeout]" in expired.stderr
    assert "1-second wall-clock limit" in expired.stderr
    assert not expired.stdout
    assert lock_path.read_bytes() == lock_before
    assert index_path.read_bytes() == index_before

    validated, query_count = validate_with_delayed_probe(5)
    assert validated.returncode == 0, validated.stdout + validated.stderr
    assert "Config is valid" in validated.stdout
    # The validation and warning passes may capture membership independently.
    # Neither should return to a repository configuration probe per component.
    assert 1 <= query_count < 65

    verified = run_cli_in_process(
        tmp_path, "verify", "--source", "head", "--transitive", "--format", "json",
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert not json.loads(verified.stdout)["issues"]

    preview = run_cli_in_process(
        tmp_path, "generate", "--source", "head", "--dry-run", "--format", "json",
    )
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert json.loads(preview.stdout) == json.loads(lock_before)
    assert lock_path.read_bytes() == lock_before
