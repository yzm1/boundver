#!/usr/bin/env python3
"""Validate and extract the exact changelog section for a release tag."""

from __future__ import annotations

import argparse
import datetime as dt
import re
from pathlib import Path
from typing import Optional, Sequence


_TAG_RE = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
_HEADING_RE = re.compile(r"(?m)^## \[([^\]]+)\](.*)$")


def extract_release_notes(changelog: str, tag: str) -> str:
    """Return one non-empty, current release section or raise ``ValueError``."""
    tag_match = _TAG_RE.fullmatch(tag)
    if tag_match is None:
        raise ValueError(f"invalid release tag: {tag!r}")
    version = tag.removeprefix("v")
    headings = list(_HEADING_RE.finditer(changelog))
    for item in headings:
        label = item.group(1)
        suffix = item.group(2)
        if label == "Unreleased":
            if suffix.strip():
                raise ValueError(
                    "CHANGELOG.md Unreleased heading must be exactly '## [Unreleased]'"
                )
            continue
        if re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", label) is None:
            raise ValueError(f"CHANGELOG.md has invalid release heading: {item.group(0)!r}")
        date_match = re.fullmatch(r"\s+-\s+(\d{4}-\d{2}-\d{2})\s*", suffix)
        if date_match is None:
            raise ValueError(
                f"CHANGELOG.md release section for {label} must include an ISO date"
            )
        try:
            dt.date.fromisoformat(date_match.group(1))
        except ValueError as exc:
            raise ValueError(
                f"CHANGELOG.md release section for {label} has an invalid ISO date"
            ) from exc

    unreleased = [item for item in headings if item.group(1) == "Unreleased"]
    releases = [item for item in headings if item.group(1) != "Unreleased"]
    matches = [item for item in releases if item.group(1) == version]

    if len(unreleased) != 1:
        raise ValueError(
            "CHANGELOG.md must contain exactly one '## [Unreleased]' section"
        )
    if len(matches) != 1:
        raise ValueError(
            f"CHANGELOG.md must contain exactly one release section for {version}; "
            f"found {len(matches)}"
        )
    release = matches[0]
    following = [item for item in releases if item.start() > unreleased[0].start()]
    if not following or following[0] is not release:
        actual = following[0].group(1) if following else "none"
        raise ValueError(
            f"CHANGELOG.md newest release is {actual}, not package release {version}; "
            "move the completed Unreleased notes into the exact release section"
        )

    next_heading = next(
        (item for item in headings if item.start() > release.start()), None
    )
    end = next_heading.start() if next_heading is not None else len(changelog)
    notes = changelog[release.end():end].strip()
    if not notes or notes == "No changes yet.":
        raise ValueError(f"CHANGELOG.md release section for {version} is empty")

    release_index = releases.index(release)
    if release_index + 1 >= len(releases):
        raise ValueError(
            f"CHANGELOG.md cannot determine the release preceding {version}"
        )
    previous_version = releases[release_index + 1].group(1)
    release_url = (
        "https://github.com/yzm1/boundver/compare/"
        f"v{previous_version}...{tag}"
    )
    link_pattern = re.compile(
        rf"(?m)^\[{re.escape(version)}\]:\s+(\S+)\s*$"
    )
    links = link_pattern.findall(changelog)
    if len(links) != 1:
        raise ValueError(
            f"CHANGELOG.md must contain exactly one link definition for {version}; "
            f"found {len(links)}"
        )
    if links[0] != release_url:
        raise ValueError(
            f"CHANGELOG.md link for {version} must compare the previous release: "
            f"{release_url}"
        )
    unreleased_pattern = re.compile(r"(?m)^\[Unreleased\]:\s+(\S+)\s*$")
    unreleased_links = unreleased_pattern.findall(changelog)
    expected_unreleased = f"https://github.com/yzm1/boundver/compare/{tag}...HEAD"
    if unreleased_links != [expected_unreleased]:
        raise ValueError(
            "CHANGELOG.md Unreleased link must be exactly " + expected_unreleased
        )
    return notes + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and extract release notes from CHANGELOG.md."
    )
    parser.add_argument("--tag", required=True, help="Exact vMAJOR.MINOR.PATCH tag")
    parser.add_argument(
        "--changelog", type=Path, default=Path("CHANGELOG.md")
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write extracted notes here; otherwise print them to stdout.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        changelog = args.changelog.read_text(encoding="utf-8")
        notes = extract_release_notes(changelog, args.tag)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    if args.output is None:
        print(notes, end="")
    else:
        args.output.write_text(notes, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
