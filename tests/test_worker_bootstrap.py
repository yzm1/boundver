"""Worker imports must follow the running artifact, including zip archives."""

import json
import os
from pathlib import Path
import subprocess
import sys
import signal
import time
import zipfile

import pytest

from boundver import _worker_bootstrap as bootstrap


@pytest.mark.parametrize("layout", ["directory", "zip"])
def test_bootstrap_runs_its_own_artifact_not_cwd_or_ambient_install(tmp_path, monkeypatch, layout):
    sources = {
        "boundver/__init__.py": "",
        "boundver/_generation_worker.py": (
            "import json,pathlib,sys\n"
            "root=pathlib.Path(sys.argv[1])\n"
            "(root/'worker.json').write_text(json.dumps({'origin':__file__,'argv':sys.argv}),encoding='utf-8')\n"
        ),
    }
    artifact = tmp_path / ("runtime archive \u03bb.pyz" if layout == "zip" else "runtime directory")
    if layout == "zip":
        with zipfile.ZipFile(artifact, "w") as archive:
            for name, text in sources.items():
                archive.writestr(name, text)
    else:
        for name, text in sources.items():
            path = artifact / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(bootstrap, "__file__", str(artifact / "boundver" / "_worker_bootstrap.py"))
    unrelated = tmp_path / "untrusted checkout"
    (unrelated / "boundver").mkdir(parents=True)
    (unrelated / "boundver" / "__init__.py").write_text("raise RuntimeError('cwd shadow imported')", encoding="utf-8")
    transfer = tmp_path / "private transfer"
    transfer.mkdir()
    completed = subprocess.run(
        [sys.executable, *bootstrap.worker_arguments(transfer)],
        cwd=unrelated, env={**os.environ, "PYTHONPATH": str(unrelated)},
        capture_output=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    report = json.loads((transfer / "worker.json").read_text(encoding="utf-8"))
    assert report["origin"].startswith(str(artifact))
    assert report["argv"] == ["boundver._generation_worker", str(transfer)]
    assert not completed.stdout and not completed.stderr


def test_bootstrap_rejects_relative_transfer_directory():
    with pytest.raises(ValueError, match="absolute"):
        bootstrap.worker_arguments(Path("relative"))


@pytest.mark.parametrize("descriptor", [-1, 0, 1, 2, True, "3"])
def test_bootstrap_rejects_invalid_lifeline_descriptors(tmp_path, descriptor):
    with pytest.raises(ValueError, match="dedicated descriptor"):
        bootstrap.worker_arguments(tmp_path, lifeline_fd=descriptor)


@pytest.mark.skipif(os.name != "posix", reason="POSIX lifeline descriptors")
def test_lifeline_descriptors_close_when_launch_fails(tmp_path, monkeypatch):
    from boundver import _posix_worker
    original = os.pipe
    created = []

    def pipe():
        descriptors = original()
        created.extend(descriptors)
        return descriptors

    def fail(*args, **kwargs):
        raise OSError("injected launch failure")

    monkeypatch.setattr(_posix_worker.os, "pipe", pipe)
    monkeypatch.setattr(_posix_worker.subprocess, "Popen", fail)
    with pytest.raises(OSError, match="injected"):
        _posix_worker.PosixWorker(tmp_path, cwd=tmp_path, env=dict(os.environ))
    assert len(created) == 2
    for descriptor in created:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process-state evidence")
def test_abrupt_controller_death_stops_worker_and_normal_descendant(tmp_path):
    package = tmp_path / "artifact" / "boundver"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "_generation_worker.py").write_text(
        "import json,os,pathlib,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "(pathlib.Path(sys.argv[1])/'ready.json').write_text(json.dumps([os.getpid(),child.pid,os.getpgrp()]))\n"
        "time.sleep(60)\n", encoding="utf-8",
    )
    transfer = tmp_path / "transfer"
    transfer.mkdir()
    controller_code = (
        "import json,os,pathlib,sys,time\n"
        "from boundver import _worker_bootstrap as bootstrap\n"
        "from boundver._posix_worker import PosixWorker\n"
        "bootstrap.__file__=str(pathlib.Path(sys.argv[1])/'boundver'/'_worker_bootstrap.py')\n"
        "root=pathlib.Path(sys.argv[2])\n"
        "worker=PosixWorker(root,cwd=root,env=dict(os.environ))\n"
        "(root/'owned.json').write_text(json.dumps(worker.pid))\n"
        "time.sleep(60)\n"
    )
    controller = subprocess.Popen(
        [sys.executable, "-I", "-c", controller_code, str(package.parent), str(transfer)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    owned_pid = None
    try:
        deadline = time.monotonic() + 10
        evidence = None
        while time.monotonic() < deadline:
            try:
                owned_pid = json.loads((transfer / "owned.json").read_text())
                evidence = json.loads((transfer / "ready.json").read_text())
                break
            except (FileNotFoundError, json.JSONDecodeError):
                assert controller.poll() is None
                time.sleep(0.01)
        assert evidence is not None, "worker did not reach the readiness barrier"
        worker_pid, child_pid, group = evidence
        assert group == worker_pid == owned_pid
        assert controller.poll() is None  # Its lifeline writer is still open.
        controller.kill()
        controller.wait(timeout=5)

        def running(pid):
            try:
                record = Path(f"/proc/{pid}/stat").read_text()
            except (FileNotFoundError, ProcessLookupError):
                return False
            # An orphan zombie has stopped executing and closed its handles;
            # init's eventual reap is not controller-owned work.
            return record[record.rfind(")") + 2:].split()[0] not in {"Z", "X"}

        deadline = time.monotonic() + 5
        while (running(worker_pid) or running(child_pid)) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not running(worker_pid)
        assert not running(child_pid)
    finally:
        if controller.poll() is None:
            controller.kill()
        controller.wait(timeout=5)
        if owned_pid is not None:
            try:
                os.killpg(owned_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if controller.stderr:
            controller.stderr.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job ownership")
def test_windows_controller_death_closes_job_and_stops_descendants(tmp_path):
    import ctypes
    from ctypes import wintypes
    worker_code = (
        "import json,os,pathlib,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "(pathlib.Path(sys.argv[1])/'ready.json').write_text(json.dumps([os.getpid(),child.pid]))\n"
        "time.sleep(60)\n"
    )
    controller_code = (
        "import os,pathlib,sys,time\n"
        "from boundver._windows_job import WindowsJob\n"
        "root=pathlib.Path(sys.argv[1])\n"
        "job=WindowsJob()\n"
        "worker=job.launch_python(['-c',sys.argv[2],str(root)],cwd=str(root),env=dict(os.environ))\n"
        "time.sleep(60)\n"
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handles = []
    controller = subprocess.Popen(
        [sys.executable, "-I", "-c", controller_code, str(tmp_path), worker_code],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        evidence = None
        while time.monotonic() < deadline:
            try:
                evidence = json.loads((tmp_path / "ready.json").read_text())
                break
            except (FileNotFoundError, json.JSONDecodeError):
                assert controller.poll() is None
                time.sleep(0.01)
        assert evidence is not None, "worker did not reach the readiness barrier"
        for pid in evidence:
            handle = kernel.OpenProcess(0x00100001, False, pid)  # SYNCHRONIZE | PROCESS_TERMINATE
            assert handle, ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            assert kernel.WaitForSingleObject(handle, 0) == 258
        assert controller.poll() is None
        controller.kill()
        controller.wait(timeout=5)
        assert all(kernel.WaitForSingleObject(handle, 5000) == 0 for handle in handles)
    finally:
        if controller.poll() is None:
            controller.kill()
        controller.wait(timeout=5)
        for handle in handles:
            if kernel.WaitForSingleObject(handle, 0) == 258:
                kernel.TerminateProcess(handle, 1)
                kernel.WaitForSingleObject(handle, 5000)
            kernel.CloseHandle(handle)
        if controller.stderr:
            controller.stderr.close()
