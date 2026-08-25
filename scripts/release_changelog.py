#!/usr/bin/env python3
"""Validate and extract the exact changelog section for a release tag."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import stat
from pathlib import Path
from typing import Optional, Sequence


_TAG_RE = re.compile(
    r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
)
_HEADING_RE = re.compile(r"(?m)^## \[([^\]]+)\](.*)$")
_MODES = ("post-release", "pre-tag")
_UPGRADE_CONTRACT_REQUIRED_FROM = (0, 14, 0)
_UPGRADE_CONTRACT_FIELD_LINE_RE = re.compile(
    r"^- (?:Semantic config|Lock schema|Fingerprint compatibility|"
    r"Lock regeneration): `[^`\r\n]+`\s*$"
)
MAX_CHANGELOG_BYTES = 8 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


def version_at_least(parts: Sequence[str], minimum: Sequence[int]) -> bool:
    """Compare decimal version fields without converting untrusted integers."""
    if len(parts) != len(minimum):
        raise ValueError("version field count does not match the comparison target")
    for raw_part, minimum_part in zip(parts, minimum):
        if not isinstance(raw_part, str) or not raw_part.isdigit():
            raise ValueError("version fields must contain only decimal digits")
        normalized = raw_part.lstrip("0") or "0"
        expected = str(minimum_part)
        if len(normalized) != len(expected):
            return len(normalized) > len(expected)
        if normalized != expected:
            return normalized > expected
    return True


def parse_upgrade_contract(notes: str, *, required: bool = True) -> dict[str, str]:
    """Parse the machine-checkable upgrade contract from release notes."""
    headings = list(re.finditer(r"(?m)^### Upgrade contract\s*$", notes))
    if not headings:
        if required:
            raise ValueError(
                "release notes must include exactly one '### Upgrade contract' "
                "section"
            )
        return {}
    if len(headings) != 1:
        raise ValueError(
            "release notes must include exactly one '### Upgrade contract' section"
        )
    if notes[: headings[0].start()].strip():
        raise ValueError(
            "release notes must start with the '### Upgrade contract' section"
        )
    start = headings[0].end()
    following = re.search(r"(?m)^#{2,3} ", notes[start:])
    end = start + following.start() if following else len(notes)
    section = notes[start:end]
    fields = {
        "semantic_config": r"(?m)^- Semantic config: `([^`]+)`\s*$",
        "lock_schema": r"(?m)^- Lock schema: `([^`]+)`\s*$",
        "fingerprints": (
            r"(?m)^- Fingerprint compatibility: "
            r"`(digest-neutral|digest-changing)`\s*$"
        ),
        "regeneration": (
            r"(?m)^- Lock regeneration: "
            r"`(not-required|required|conditional)`\s*$"
        ),
    }
    result: dict[str, str] = {}
    for field, pattern in fields.items():
        matches = re.findall(pattern, section)
        if len(matches) != 1:
            label = field.replace("_", " ")
            raise ValueError(
                f"release upgrade contract must define {label} exactly once"
            )
        result[field] = matches[0]
    if (
        result["fingerprints"] == "digest-neutral"
        and result["regeneration"] != "not-required"
    ):
        raise ValueError(
            "digest-neutral release notes must declare lock regeneration "
            "`not-required`"
        )
    if (
        result["fingerprints"] == "digest-changing"
        and result["regeneration"] == "not-required"
    ):
        raise ValueError(
            "digest-changing release notes cannot declare lock regeneration "
            "`not-required`"
        )
    return result


def _is_windows_reparse_point(identity: os.stat_result) -> bool:
    attributes = getattr(identity, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _changed(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or before.st_mode != after.st_mode
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    )


def _read_changelog(path: Path) -> str:
    """Read a stable regular changelog through a one-byte sentinel."""
    try:
        initial = path.lstat()
        if not stat.S_ISREG(initial.st_mode) or _is_windows_reparse_point(initial):
            raise ValueError(f"changelog is not a regular file: {path}")
        if initial.st_size > MAX_CHANGELOG_BYTES:
            raise ValueError(
                f"changelog exceeds the {MAX_CHANGELOG_BYTES}-byte limit: {path}"
            )
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or _changed(initial, opened):
                raise ValueError(f"changelog changed while opening: {path}")
            content = bytearray()
            while True:
                remaining = MAX_CHANGELOG_BYTES - len(content)
                chunk = stream.read(min(_READ_CHUNK_BYTES, remaining + 1))
                if not chunk:
                    break
                if len(chunk) > remaining:
                    raise ValueError(
                        "changelog exceeds the "
                        f"{MAX_CHANGELOG_BYTES}-byte limit: {path}"
                    )
                content.extend(chunk)
            finished = os.fstat(stream.fileno())
        current = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"changelog disappeared while reading: {path}") from exc
    if (
        not stat.S_ISREG(current.st_mode)
        or _is_windows_reparse_point(current)
        or _changed(opened, finished)
        or _changed(finished, current)
        or finished.st_size != len(content)
    ):
        raise ValueError(f"changelog changed while reading: {path}")
    return bytes(content).decode("utf-8")


def extract_release_notes(
    changelog: str, tag: str, *, mode: str = "post-release"
) -> str:
    """Return one non-empty, current release section or raise ``ValueError``."""
    if mode not in _MODES:
        raise ValueError(
            f"invalid changelog validation mode {mode!r}; "
            f"expected one of: {', '.join(_MODES)}"
        )
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
        if re.fullmatch(
            r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
            label,
        ) is None:
            raise ValueError(f"CHANGELOG.md has invalid release heading: {item.group(0)!r}")
        date_match = re.fullmatch(
            r"\s+-\s+([0-9]{4}-[0-9]{2}-[0-9]{2})\s*", suffix
        )
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
    unreleased_notes = changelog[unreleased[0].end() : release.start()].strip()
    if mode == "pre-tag" and unreleased_notes:
        raise ValueError(
            "CHANGELOG.md Unreleased section must be empty before creating the "
            f"{tag} release tag"
        )

    next_heading = next(
        (item for item in headings if item.start() > release.start()), None
    )
    end = next_heading.start() if next_heading is not None else len(changelog)
    notes = (
        changelog[release.end():end]
        .strip()
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    if not notes or notes == "No changes yet.":
        raise ValueError(f"CHANGELOG.md release section for {version} is empty")
    upgrade_contract = parse_upgrade_contract(
        notes,
        required=version_at_least(
            tag_match.groups(),
            _UPGRADE_CONTRACT_REQUIRED_FROM,
        ),
    )
    if upgrade_contract:
        heading = re.search(r"(?m)^### Upgrade contract\s*$", notes)
        assert heading is not None
        following = re.search(r"(?m)^#{2,3} ", notes[heading.end():])
        contract_end = (
            heading.end() + following.start()
            if following is not None
            else len(notes)
        )
        contract_lines = notes[:contract_end].splitlines()
        contract_remainder = "\n".join(
            line
            for line in contract_lines
            if line.strip() != "### Upgrade contract"
            and _UPGRADE_CONTRACT_FIELD_LINE_RE.fullmatch(line) is None
        )
        substantive_notes = (
            contract_remainder + "\n" + notes[contract_end:]
        ).strip()
        if not substantive_notes or substantive_notes == "No changes yet.":
            raise ValueError(
                f"CHANGELOG.md release section for {version} is empty"
            )

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
        "--mode",
        choices=_MODES,
        default="post-release",
        help="pre-tag additionally requires an empty Unreleased section",
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
        changelog = _read_changelog(args.changelog)
        notes = extract_release_notes(changelog, args.tag, mode=args.mode)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    if args.output is None:
        print(notes, end="")
    else:
        args.output.write_text(notes, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
