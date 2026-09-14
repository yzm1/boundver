"""Regression tests for the generated CLI reference."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "generate_cli_reference.py"
SPEC = importlib.util.spec_from_file_location(
    "boundver_generate_cli_reference", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
generator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = generator
SPEC.loader.exec_module(generator)


def test_committed_cli_reference_is_current() -> None:
    assert (ROOT / "docs" / "cli-reference.md").read_text(
        encoding="utf-8"
    ) == generator.render()


def test_generated_cli_reference_has_no_trailing_whitespace() -> None:
    assert all(
        line == line.rstrip(" \t") for line in generator.render().splitlines()
    )


def test_usage_normalization_folds_only_an_orphan_ellipsis() -> None:
    class Parser:
        prog = "boundver"

        @staticmethod
        def format_usage() -> str:
            return "usage: boundver [-h]\n                {one,two}\n                ...\n"

    assert generator._format_usage(Parser()) == [
        "usage: boundver [-h]",
        "                {one,two} ...",
    ]
