#!/usr/bin/env python3
"""Render the Homebrew formula for one immutable boundver release asset."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Sequence


VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
MAX_FORMULA_BYTES = 16 * 1024


def render_formula(version: str, pyz_sha256: str) -> str:
    if VERSION_RE.fullmatch(version) is None:
        raise ValueError("version must be an exact MAJOR.MINOR.PATCH release")
    if SHA256_RE.fullmatch(pyz_sha256) is None:
        raise ValueError("pyz SHA-256 must be 64 lowercase hexadecimal characters")
    formula = f'''class Boundver < Formula
  desc "Classify contract drift and downstream impact across polyglot repositories"
  homepage "https://github.com/yzm1/boundver"
  url "https://github.com/yzm1/boundver/releases/download/v{version}/boundver-{version}.pyz"
  sha256 "{pyz_sha256}"
  license "MIT"

  depends_on "python@3.14"

  def install
    python = formula_opt_bin("python@3.14")/"python3.14"
    bin.mkpath
    system python, "-m", "zipapp", "boundver-{version}.pyz",
           "--output", bin/"boundver", "--python", python
  end

  test do
    assert_match "{version}", shell_output("#{{bin}}/boundver --version")
  end
end
'''
    if len(formula.encode("utf-8")) > MAX_FORMULA_BYTES:
        raise ValueError("rendered formula exceeds its size limit")
    return formula


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--pyz-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        formula = render_formula(args.version, args.pyz_sha256)
        _atomic_write(args.output.resolve(), formula)
    except (OSError, ValueError) as error:
        print(f"Homebrew formula error: {error}", file=sys.stderr)
        return 1
    print(f"Rendered {args.output} for boundver {args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
