"""Owned POSIX worker session, not containment of deliberately detached code.

Native custom providers are trusted Python. A process that creates another
session can escape this group; callers must not claim otherwise.
"""

import os
from pathlib import Path
import signal
import subprocess
import sys


class PosixWorker:
    def __init__(self, directory: Path, *, cwd: Path, env: dict[str, str]):
        if os.name != "posix":
            raise OSError("POSIX worker sessions are unavailable")
        if not directory.is_absolute() or not cwd.is_absolute() or not os.path.isabs(sys.executable):
            raise ValueError("Worker interpreter and paths must be absolute")
        worker_command = [sys.executable, "-I", "-m", "boundver._generation_worker", str(directory)]
        self.process = subprocess.Popen(
            worker_command, cwd=cwd, env=dict(env),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True,
        )
        self.pid = self.process.pid

    def poll(self):
        return self.process.poll()

    def terminate_group(self):
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def group_exists(self) -> bool:
        try:
            os.killpg(self.pid, 0)
        except ProcessLookupError:
            return False
        return True

    def wait(self, timeout: float):
        return self.process.wait(timeout=timeout)
