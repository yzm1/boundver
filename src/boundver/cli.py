"""Package CLI entrypoint for boundver."""

from pathlib import Path
import runpy


def main() -> None:
    """Execute the repository's boundary_lock.py CLI."""
    root = Path(__file__).resolve().parents[2]
    script = root / "boundary_lock.py"
    runpy.run_path(str(script), run_name="__main__")
