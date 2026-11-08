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


def worker_arguments(directory: Path) -> list[str]:
    if not directory.is_absolute():
        raise ValueError("Worker transfer directory must be absolute")
    # For an installed wheel this is site-packages; for a zipapp it is the
    # archive itself. pathlib does not need to traverse inside a zip file.
    runtime_root = Path(__file__).absolute().parent.parent
    return ["-I", "-c", WORKER_BOOTSTRAP, str(runtime_root), str(directory)]
