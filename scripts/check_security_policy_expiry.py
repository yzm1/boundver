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
    blocks = re.split(r"^  - id: CVE-[0-9]{4}-[0-9]+$", text, flags=re.MULTILINE)
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
        # Normalize Windows checkouts without accepting alternate date syntax.
        text = data.decode("utf-8").replace("\r\n", "\n")
        check_policy(text, datetime.datetime.now(datetime.timezone.utc).date())
    except (OSError, UnicodeError, ValueError) as error:
        print(f"Container security policy preflight failed: {error}", file=sys.stderr)
        return 1
    print("Container exception dates are current; container scans are still required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
