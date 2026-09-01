#!/usr/bin/env python3
"""Fail closed on drift in the public GitLab CI/CD component contract."""

from __future__ import annotations

import os
import re
import stat
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
    "set -- boundver verify",
    "set -- boundver review",
    "--format plan",
    '--summary-file "$summary_file"',
    'GIT_DEPTH: "$[[ inputs.history-depth ]]"',
    "boundver-result.json",
    "boundver-summary.md",
    "when: always",
    '--format json',
    '"$@"',
)


def component_errors(path: Path = COMPONENT) -> list[str]:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            return [f"component must be a regular file: {path}"]
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                return [f"component changed before its bounded read: {path}"]
            data = stream.read(MAX_COMPONENT_BYTES + 1)
            after_open = os.fstat(stream.fileno())
        after_path = path.lstat()
    except OSError as error:
        return [f"cannot read {path}: {error}"]
    if len(data) > MAX_COMPONENT_BYTES:
        return [f"component exceeds the {MAX_COMPONENT_BYTES}-byte limit"]
    def identity(item):
        return (
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_size,
            item.st_mtime_ns,
        )
    if (
        identity(before) != identity(opened)
        or identity(opened) != identity(after_open)
        or identity(after_open) != identity(after_path)
    ):
        return [f"component changed during its bounded read: {path}"]
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
    if "git config --global" in text or "git rev-parse --is-shallow-repository" in text:
        errors.append(
            "component must leave Git trust and history inspection to boundver's "
            "isolated Git transport"
        )
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
