"""The cheap CI preflight must fail before stale policies start paid jobs."""

import datetime
import importlib.util
import io
from pathlib import Path
import unittest
from unittest import mock


PATH = Path(__file__).resolve().parents[1] / "scripts/check_security_policy_expiry.py"
SPEC = importlib.util.spec_from_file_location("security_policy_expiry", PATH)
assert SPEC is not None and SPEC.loader is not None
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


class SecurityPolicyExpiryTests(unittest.TestCase):
    def test_dates_are_bounded_and_fail_on_the_expiry_day(self):
        today = datetime.date(2026, 10, 4)
        for days in (-1, 0, 1, 14, 15):
            with self.subTest(days=days):
                expiry = today + datetime.timedelta(days=days)
                text = f"  - id: CVE-2026-1234\n    expired_at: {expiry}\n"
                if 1 <= days <= 14:
                    POLICY.check_policy(text, today)
                else:
                    with self.assertRaises(ValueError):
                        POLICY.check_policy(text, today)

    def test_every_entry_and_date_must_be_explicit(self):
        prefix = "  - id: CVE-2026-1234\n"
        cases = (
            "", prefix, prefix + "    expired_at: 2026-02-30\n",
            prefix + '    expired_at: "2026-10-10"\n',
            prefix + "    expired_at: 20261010\n",
            prefix + "    expired_at: 2026-10-10\n    expired_at: 2026-10-11\n",
            prefix + "    expired_at: 2026-10-10\n  - id: CVE-2026-5678\n",
            prefix + "    expired_at: 2026-10-10\n    expired_at: 2026-10-11\n"
            + "  - id: CVE-2026-5678\n",
        )
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ValueError):
                POLICY.check_policy(text, datetime.date(2026, 10, 4))

    def test_main_bounds_reads_and_fails_closed(self):
        cases = (
            (b"invalid policy", 1),
            (b"\xff", 1),
            (b"x" * (POLICY.MAX_POLICY_BYTES + 1), 1),
            (b"  - id: CVE-2026-1234\r\n    expired_at: 2026-10-05\r\n", 0),
        )
        for data, expected in cases:
            with self.subTest(data=data[:32]):
                opener = mock.mock_open(read_data=data)
                clock = mock.Mock(wraps=datetime.datetime)
                clock.now.return_value = datetime.datetime(
                    2026, 10, 4, tzinfo=datetime.timezone.utc
                )
                with (
                    mock.patch.object(Path, "open", opener),
                    mock.patch.object(POLICY.datetime, "datetime", clock),
                    mock.patch.object(POLICY.sys, "stdout", io.StringIO()),
                    mock.patch.object(POLICY.sys, "stderr", io.StringIO()),
                ):
                    self.assertEqual(POLICY.main(), expected)
                opener().read.assert_called_once_with(POLICY.MAX_POLICY_BYTES + 1)

    def test_missing_policy_fails_closed(self):
        with (
            mock.patch.object(Path, "open", side_effect=OSError("missing")),
            mock.patch.object(POLICY.sys, "stderr", io.StringIO()),
        ):
            self.assertEqual(POLICY.main(), 1)
