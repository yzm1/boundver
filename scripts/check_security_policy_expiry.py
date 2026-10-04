#!/usr/bin/env python3
"""Reject stale container exceptions before starting paid CI runners.

This conservative, stdlib-only preflight checks the checked-in policy's date
fields. It does not replace YAML validation or either container scan.
"""

from __future__ import annotations

import datetime
from pathlib import Path
import re
import sys


POLICY = Path(__file__).resolve().parents[1] / ".trivyignore.yaml"
MAX_POLICY_BYTES = 64 * 1024


def check_policy(text: str, today: datetime.date) -> None:
    # The checked-in subset allows LF and CRLF only. Reject other separators
    # rather than letting Python and the downstream YAML scanner disagree.
    text = text.replace("\r\n", "\n")
    if any(separator in text for separator in "\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
        raise ValueError("unsupported container exception line break")
    # Accept only the canonical checked-in subset, not arbitrary YAML. In
    # particular, a quoted/flow-style ID must not fold into a prior entry and
    # inherit its expiry. Trivy remains the authoritative YAML/scan consumer.
    lines = [line for line in text.split("\n")
             if line.strip() and not line.lstrip().startswith("#")]
    if not lines or lines[0] != "vulnerabilities:":
        raise ValueError("expected one canonical vulnerabilities mapping")
    allowed = (
        r"  - id: CVE-[0-9]{4}-[0-9]+",
        r"    purls:",
        r"      - pkg:deb/debian/[A-Za-z0-9.+-]+",
        r"    expired_at: [0-9]{4}-[0-9]{2}-[0-9]{2}",
        r"    statement: [A-Za-z][^\r\n]*",
    )
    if len(lines) < 2 or not re.fullmatch(allowed[0], lines[1]):
        raise ValueError("expected a canonical CVE entry")
    for line in lines[1:]:
        if not any(re.fullmatch(pattern, line) for pattern in allowed):
            raise ValueError("unsupported container exception syntax")
    canonical = "\n".join(lines)
    blocks = re.split(r"^  - id: CVE-[0-9]{4}-[0-9]+$", canonical, flags=re.MULTILINE)
    if len(blocks) == 1 or re.search(r"expired_at:", blocks[0]):
        raise ValueError("each exception must have one explicit expiry date")
    for block in blocks[1:]:
        fields = re.findall(r"^    expired_at: (.*)$", block, re.MULTILINE)
        if len(fields) != 1:
            raise ValueError("each exception must have one explicit expiry date")
        value = fields[0]
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("exception expiry must be an unquoted YYYY-MM-DD date")
        expiry = datetime.date.fromisoformat(value)
        if not today < expiry <= today + datetime.timedelta(days=14):
            raise ValueError(f"exception expiry {value} is stale or exceeds 14 days")


def main() -> int:
    try:
        with POLICY.open("rb") as stream:
            data = stream.read(MAX_POLICY_BYTES + 1)
        if len(data) > MAX_POLICY_BYTES:
            raise ValueError("container exception policy is oversized")
        text = data.decode("utf-8")
        check_policy(text, datetime.datetime.now(datetime.timezone.utc).date())
    except (OSError, UnicodeError, ValueError) as error:
        print(f"Container security policy preflight failed: {error}", file=sys.stderr)
        return 1
    print("Container exception dates are current; container scans are still required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
