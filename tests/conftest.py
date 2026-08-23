"""Pytest configuration for local imports."""

import tempfile
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for candidate in (str(SRC), str(ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)


_BASE_TEMPORARY_DIRECTORY = tempfile.TemporaryDirectory


class _WindowsRetryingTemporaryDirectory(_BASE_TEMPORARY_DIRECTORY):
    """Retry only transient Windows sharing violations during test cleanup."""

    _CLEANUP_DELAYS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.4)

    def cleanup(self):
        for delay in self._CLEANUP_DELAYS:
            try:
                return super().cleanup()
            except PermissionError as error:
                if getattr(error, "winerror", None) not in {5, 32}:
                    raise
                time.sleep(delay)
        return super().cleanup()


if sys.platform == "win32":
    # Git for Windows and real-time scanners can retain a just-finished test
    # repository for a few milliseconds. Persistent locks still fail after the
    # bounded retry window instead of being hidden.
    tempfile.TemporaryDirectory = _WindowsRetryingTemporaryDirectory
