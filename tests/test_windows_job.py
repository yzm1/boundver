"""Real kernel job ownership; no process-name or global tree termination."""

import os
import subprocess
import sys
import time

import pytest

from boundver._windows_job import WindowsJob


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows kernel jobs")


def test_empty_job_is_non_inheritable_and_closes_idempotently():
    import ctypes
    from ctypes import wintypes
    with WindowsJob() as job:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetHandleInformation.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetHandleInformation.restype = wintypes.BOOL
        flags = wintypes.DWORD()
        assert kernel.GetHandleInformation(job.handle, ctypes.byref(flags))
        assert not flags.value & 1  # HANDLE_FLAG_INHERIT
        assert job.active_processes == 0
    job.close()
    with pytest.raises(OSError, match="closed"):
        job.terminate()


def test_suspended_worker_can_be_assigned_and_killed_without_executing(tmp_path):
    marker = tmp_path / "executed"
    process = subprocess.Popen(
        [sys.executable, "-c", "import pathlib,sys;pathlib.Path(sys.argv[1]).touch()", str(marker)],
        creationflags=0x00000004,  # CREATE_SUSPENDED: no user code before assignment
    )
    try:
        with WindowsJob() as job:
            job.assign(int(process._handle))
            assert job.active_processes == 1
        assert process.wait(timeout=5) is not None
        assert not marker.exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_job_terminates_child_even_after_parent_exits():
    # The trusted probe cannot spawn until the controller assigns its job and
    # sends a byte. Production launch must instead be assigned while suspended.
    probe = (
        "import subprocess,sys;sys.stdin.read(1);"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        "print('spawned',flush=True)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", probe], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, text=True,
    )
    try:
        with WindowsJob() as job:
            job.assign(int(process._handle))
            output, _ = process.communicate("x", timeout=5)
            assert output.strip() == "spawned"
            assert process.returncode == 0
            assert job.active_processes >= 1
            job.terminate()
            end = time.monotonic() + 5
            while job.active_processes and time.monotonic() < end:
                time.sleep(0.01)
            assert job.active_processes == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if process.stdin:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()


def test_invalid_assignment_does_not_destroy_job():
    with WindowsJob() as job:
        with pytest.raises(OSError):
            job.assign(0)
        assert job.active_processes == 0
