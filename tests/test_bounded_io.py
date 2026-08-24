"""Tests for the shared local-file byte guardrail."""

import tempfile
import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from boundver._bounded_io import FileSizeLimitError, read_bounded_file


class BoundedFileReadTests(unittest.TestCase):
    def test_reads_file_at_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "value.bin"
            path.write_bytes(b"abcd")
            self.assertEqual(read_bounded_file(path, 4), b"abcd")

    def test_rejects_file_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "value.bin"
            path.write_bytes(b"abcde")
            with self.assertRaises(FileSizeLimitError) as raised:
                read_bounded_file(path, 4)
            self.assertEqual(raised.exception.size, 5)
            self.assertEqual(raised.exception.limit, 4)

    def test_rechecks_size_after_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "growing.bin"
            path.write_bytes(b"abcde")
            real_fstat = os.fstat
            calls = 0

            def underreport_first_size(fd):
                nonlocal calls
                calls += 1
                result = real_fstat(fd)
                if calls != 1:
                    return result
                return SimpleNamespace(
                    st_mode=result.st_mode,
                    st_size=1,
                    st_mtime_ns=result.st_mtime_ns,
                    st_dev=result.st_dev,
                    st_ino=result.st_ino,
                )

            with patch("boundver._bounded_io.os.fstat", side_effect=underreport_first_size):
                with self.assertRaises(FileSizeLimitError) as raised:
                    read_bounded_file(path, 4)
        self.assertEqual(raised.exception.size, 5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
