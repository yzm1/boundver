"""Contained computation controller; deliberately separate from publication."""

import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import time

from ._bounded_io import read_bounded_file
from ._generation_control import GenerationControl, GenerationState
from ._generation_protocol import MAX_GENERATION_RESULT_HEADER_BYTES, decode_result
from ._generation_worker import (
    MAX_GENERATION_ERROR_BYTES, MAX_GENERATION_REQUEST_BYTES,
    _validate_request, write_transfer,
)
from ._lockfile import MAX_LOCKFILE_BYTES
from ._posix_worker import PosixJob
from ._structured_data import strict_json_loads
from ._utils import ConfigError, _bounded_diagnostic_text
from ._windows_job import WindowsJob


MAX_GENERATION_CLEANUP_SECONDS = 5


def _require_computing(control: GenerationControl) -> None:
    state = control.state
    if state is not GenerationState.COMPUTING:
        raise ConfigError(f"Generation did not publish: {state.value}")


def run_windows_generation(data: bytes, *, control: GenerationControl, running_version: str) -> dict:
    """Return a validated lock only after all owned Windows work has stopped.

    The caller creates control before source capture, and must use its publish
    method for admission after this function returns. No destination is opened.
    """
    if os.name != "nt":
        raise ConfigError("Windows generation containment is unavailable")
    return _run_generation(data, control=control, running_version=running_version, job_factory=WindowsJob)


def run_generation(data: bytes, *, control: GenerationControl, running_version: str) -> dict:
    if os.name == "nt":
        return run_windows_generation(data, control=control, running_version=running_version)
    if os.name != "posix":
        raise ConfigError("Generation process containment is unavailable")
    return _run_generation(data, control=control, running_version=running_version, job_factory=PosixJob)


def _run_generation(data: bytes, *, control: GenerationControl, running_version: str, job_factory) -> dict:
    _require_computing(control)
    if not isinstance(data, bytes) or len(data) > MAX_GENERATION_REQUEST_BYTES:
        raise ConfigError("Invalid generation request size")
    invocation = strict_json_loads(data.decode("utf-8"))
    _validate_request(invocation)
    if invocation["running_version"] != running_version:
        raise ConfigError("Generation runtime mismatch")
    _require_computing(control)
    directory = Path(tempfile.mkdtemp(prefix="boundver-generation-"))
    identity = directory.stat()
    cleanup_confirmed = True
    try:
        write_transfer(directory, "request.json", data)
        with job_factory() as job:
            process = None
            try:
                _require_computing(control)
                cleanup_confirmed = False
                process = job.launch_python(
                    ["-I", "-m", "boundver._generation_worker", str(directory)],
                    cwd=invocation["repo_root"], env=dict(os.environ),
                )
                while process.poll() is None:
                    _require_computing(control)
                    time.sleep(0.01)
                status = process.poll()
            except KeyboardInterrupt:
                control.cancel()
                raise
            except BaseException:
                control.fail()
                raise
            finally:
                # Also remove descendants left after a successful worker exit.
                # Job termination and process-handle signalling are distinct.
                try:
                    job.terminate()
                    end = time.monotonic() + MAX_GENERATION_CLEANUP_SECONDS
                    while (process is not None and process.poll() is None) or job.active_processes:
                        if time.monotonic() >= end:
                            raise ConfigError(f"Generation cleanup unconfirmed; transfer retained at {directory}")
                        time.sleep(0.01)
                    cleanup_confirmed = True
                finally:
                    if process is not None:
                        process.close()
        _require_computing(control)
        if status != 0:
            detail = "worker failed without a diagnostic"
            try:
                error = strict_json_loads(read_bounded_file(
                    directory / "error.json", MAX_GENERATION_ERROR_BYTES,
                    trusted_root=directory, path_label="worker error",
                ).decode("utf-8"))
                if isinstance(error, dict) and set(error) == {"error"} and isinstance(error["error"], str):
                    detail = _bounded_diagnostic_text(error["error"], max_chars=1024)
            except (OSError, ValueError):
                pass
            raise ConfigError(f"Generation worker failed: {detail}")
        result = read_bounded_file(
            directory / "result.bin", 4 + MAX_GENERATION_RESULT_HEADER_BYTES + MAX_LOCKFILE_BYTES,
            trusted_root=directory, path_label="generation worker result",
        )
        lock = decode_result(result, request_digest=hashlib.sha256(data).hexdigest(), running_version=running_version)
        if not control.result_ready():
            raise ConfigError(f"Generation did not publish: {control.state.value}")
        return lock
    except KeyboardInterrupt:
        control.cancel()
        raise
    except BaseException:
        control.fail()
        raise
    finally:
        if cleanup_confirmed:
            current = directory.lstat()
            if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                control.fail()
                raise ConfigError("Generation transfer directory identity changed; cleanup refused")
            shutil.rmtree(directory)
