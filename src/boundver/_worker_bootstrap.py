"""Launch the worker from the controller's own package root or zip archive.

Never derive this import root from the repository being inspected or from its
configuration. Isolated Python startup ignores cwd/PYTHONPATH; the fixed shim
adds only the runtime artifact already executing in the controller.
"""

from pathlib import Path


WORKER_BOOTSTRAP = (
    "import runpy,sys;"
    "sys.path.insert(0,sys.argv[1]);"
    "sys.argv=['boundver._generation_worker',*sys.argv[2:]];"
    "runpy.run_module('boundver._generation_worker',run_name='__main__')"
)

POSIX_WORKER_BOOTSTRAP = (
    "import os,signal,stat,sys,threading\n"
    "if os.name!='posix' or os.getpgrp()!=os.getpid(): raise SystemExit(2)\n"
    "_lifeline=int(sys.argv.pop())\n"
    "if _lifeline<3 or not stat.S_ISFIFO(os.fstat(_lifeline).st_mode): raise SystemExit(2)\n"
    "def _watch_controller():\n"
    "    try:\n"
    "        while os.read(_lifeline,1): pass\n"
    "    finally:\n"
    "        os.killpg(os.getpid(),signal.SIGKILL)\n"
    "threading.Thread(target=_watch_controller,daemon=True).start()\n"
    + WORKER_BOOTSTRAP
)


def worker_arguments(directory: Path, *, lifeline_fd=None) -> list[str]:
    if not directory.is_absolute():
        raise ValueError("Worker transfer directory must be absolute")
    # For an installed wheel this is site-packages; for a zipapp it is the
    # archive itself. pathlib does not need to traverse inside a zip file.
    runtime_root = Path(__file__).absolute().parent.parent
    if lifeline_fd is not None:
        if type(lifeline_fd) is not int or lifeline_fd < 3:
            raise ValueError("Worker lifeline must be a dedicated descriptor")
        return ["-I", "-c", POSIX_WORKER_BOOTSTRAP, str(runtime_root), str(directory), str(lifeline_fd)]
    return ["-I", "-c", WORKER_BOOTSTRAP, str(runtime_root), str(directory)]
