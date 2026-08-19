"""Regressions for streamed, bounded Git name-status parsing."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from boundver._git import (
    MAX_GIT_STATUS_FIELDS,
    _git_name_status,
    _parse_name_status_entries,
)
from boundver._utils import GuardrailError


class GitNameStatusBoundsTests(unittest.TestCase):
    def test_rename_preserves_both_identities_and_copy_only_destination(self):
        self.assertEqual(
            _parse_name_status_entries(
                iter(
                    (
                        b"R100",
                        b"old/path.txt",
                        b"new/path.txt",
                        b"C100",
                        b"source.txt",
                        b"copy.txt",
                    )
                )
            ),
            [
                ("R100", "old/path.txt"),
                ("R100", "new/path.txt"),
                ("C100", "copy.txt"),
            ],
        )

    def test_path_count_is_enforced_during_streamed_parse(self):
        with patch("boundver._git.MAX_GIT_STATUS_PATHS", 1):
            with self.assertRaisesRegex(GuardrailError, "1-path limit"):
                _parse_name_status_entries(
                    iter((b"M", b"first.txt", b"M", b"second.txt"))
                )

    def test_runner_consumes_streaming_records_instead_of_capture_output(self):
        fields = iter((b"M", b"path.txt"))
        with patch(
            "boundver._git._iter_git_nul_records",
            return_value=fields,
        ) as stream:
            self.assertEqual(
                _git_name_status(Path("repo"), ["diff", "--name-status", "-z"]),
                [("M", "path.txt")],
            )

        stream.assert_called_once()
        self.assertEqual(
            stream.call_args.kwargs["max_records"],
            MAX_GIT_STATUS_FIELDS,
        )

    def test_malformed_or_truncated_records_fail_closed(self):
        cases = (
            ((b"M",), "Truncated path"),
            ((b"R100", b"old.txt"), "Truncated rename/copy"),
            ((b"?", b"path.txt"), "Malformed Git diff status"),
            ((b"\xff", b"path.txt"), "non-ASCII"),
        )
        for fields, message in cases:
            with self.subTest(fields=fields):
                with self.assertRaisesRegex(ValueError, message):
                    _parse_name_status_entries(iter(fields))


if __name__ == "__main__":
    unittest.main()
