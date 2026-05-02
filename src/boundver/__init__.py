"""boundver package."""

__all__ = ["main"]

from importlib.metadata import PackageNotFoundError, version

from .cli import main

try:
    __version__ = version("boundver")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0+local"
