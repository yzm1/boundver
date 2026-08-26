#!/usr/bin/env python3
"""Fail closed on drift in the public GitLab CI/CD component contract."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "templates" / "boundver.yml"
MAX_COMPONENT_BYTES = 64 * 1024
REQUIRED_SNIPPETS = (
    "spec:\n  description:",
    "  component: [version]",
    'name: ghcr.io/yzm1/boundver:$[[ component.version ]]',
    'entrypoint: [""]',
    'git config --global --add safe.directory "$CI_PROJECT_DIR"',
    "set -- boundver verify",
    '--format json',
    '"$@"',
)


def component_errors(path: Path = COMPONENT) -> list[str]:
    try:
        data = path.read_bytes()
    except OSError as error:
        return [f"cannot read {path}: {error}"]
    if len(data) > MAX_COMPONENT_BYTES:
        return [f"component exceeds the {MAX_COMPONENT_BYTES}-byte limit"]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ["component is not UTF-8"]
    errors = [
        f"component is missing required contract: {snippet!r}"
        for snippet in REQUIRED_SNIPPETS
        if snippet not in text
    ]
    if text.count("\n---\n") != 1:
        errors.append("component must contain exactly one spec/document separator")
    if re.search(r"(?m)^\s*(before_script|after_script|cache|services):", text):
        errors.append("component must not introduce global execution or cache state")
    if "latest" in text.casefold():
        errors.append("component must bind its image to the resolved component version")
    return errors


def main() -> int:
    errors = component_errors()
    if errors:
        for error in errors:
            print(f"GitLab component error: {error}", file=sys.stderr)
        return 1
    print("GitLab component contract passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
