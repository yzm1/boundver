"""Tests for the shared local-file byte guardrail.

The size limit is only half of what this primitive promises. It also
refuses anything that is not a regular file, which is the guard that keeps
a device, a pipe or a socket from being read as though it were declared
content. That half had no test on any host until MUT-HASHING-206 mutated
it away and the suite stayed green, so it is asserted here twice: once
against a real non-regular file, and once against a mode no host in the
matrix can produce on demand.

Covers OBL-HASHING-118.
"""

import stat
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


class NonRegularFileTests(unittest.TestCase):
    """A descriptor that is not a regular file must be refused."""

    def test_the_null_device_is_refused(self) -> None:
        """The real thing: every supported host has a character device.

        ``os.devnull`` is ``/dev/null`` on POSIX and ``nul`` on Windows.
        Both open without blocking and both report a character-device
        mode, so this arms the guard on every host in the matrix rather
        than only where a FIFO can be created. A FIFO would be the more
        obvious fixture and is the wrong one: opening it for reading
        blocks until a writer arrives.
        """
        with self.assertRaises(ValueError) as raised:
            read_bounded_file(Path(os.devnull), 16)
        self.assertIn("Unsupported working-tree file type", str(raised.exception))

    def test_the_null_device_is_not_a_regular_file(self) -> None:
        """The premise: the refusal above is about the file type.

        Without this, a host that represented the null device as an empty
        regular file would turn the test above into an assertion about
        nothing, and it would still pass for as long as the read happened
        to fail for some other reason.
        """
        with Path(os.devnull).open("rb") as stream:
            mode = os.fstat(stream.fileno()).st_mode
        self.assertFalse(stat.S_ISREG(mode), oct(mode))

    def test_a_stable_symlink_gets_a_specific_controlled_diagnostic(self) -> None:
        """Exercise the POSIX path on every host without following a real link."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "value.bin"
            path.write_bytes(b"abcd")
            identity = path.lstat()
            symlink_identity = SimpleNamespace(
                st_mode=stat.S_IFLNK | 0o777,
                st_size=identity.st_size,
                st_mtime_ns=identity.st_mtime_ns,
                st_dev=identity.st_dev,
                st_ino=identity.st_ino,
            )
            with patch.object(Path, "lstat", return_value=symlink_identity):
                with self.assertRaises(ValueError) as raised:
                    read_bounded_file(path, 16)
        self.assertIn("file type (symlink)", str(raised.exception))

    def test_a_fifo_mode_is_refused_on_hosts_that_cannot_make_one(self) -> None:
        """The modes the matrix cannot produce, reported by a real file."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "value.bin"
            path.write_bytes(b"abcd")
            real_fstat = os.fstat

            for label, kind in (
                ("fifo", stat.S_IFIFO),
                ("socket", stat.S_IFSOCK),
                ("block device", stat.S_IFBLK),
            ):
                def report_kind(fd, kind=kind):
                    result = real_fstat(fd)
                    return SimpleNamespace(
                        st_mode=stat.S_IMODE(result.st_mode) | kind,
                        st_size=result.st_size,
                        st_mtime_ns=result.st_mtime_ns,
                        st_dev=result.st_dev,
                        st_ino=result.st_ino,
                    )

                with self.subTest(kind=label):
                    with patch(
                        "boundver._bounded_io.os.fstat",
                        side_effect=report_kind,
                    ):
                        with self.assertRaises(ValueError) as raised:
                            read_bounded_file(path, 16)
                    self.assertIn(
                        "Unsupported working-tree file type",
                        str(raised.exception),
                    )

    def test_the_same_file_reads_cleanly_unpatched(self) -> None:
        """The contrast: the refusals above are about the mode alone."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "value.bin"
            path.write_bytes(b"abcd")
            self.assertEqual(read_bounded_file(path, 16), b"abcd")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
