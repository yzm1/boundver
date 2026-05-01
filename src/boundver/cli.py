"""Package CLI entrypoint for boundver."""

from boundary_lock import main as boundary_lock_main


def main() -> None:
    """Execute the boundver CLI."""
    boundary_lock_main()
